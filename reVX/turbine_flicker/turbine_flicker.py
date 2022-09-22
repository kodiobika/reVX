# -*- coding: utf-8 -*-
"""
Turbine Flicker exclusions calculator
"""
from concurrent.futures import as_completed
import logging
import numpy as np
import os
from warnings import warn

from reV.handlers.exclusions import ExclusionLayers
from reV.supply_curve.extent import SupplyCurveExtent
from reV.supply_curve.tech_mapping import TechMapping
from reVX.handlers.geotiff import Geotiff
from reVX.setbacks.regulations import WindRegulations
from reVX.wind_dirs.mean_wind_dirs_point import MeanWindDirectionsPoint
from reVX.utilities.exclusions import (ExclusionsConverter,
                                       AbstractBaseExclusionsMerger)
from rex.resource_extraction.resource_extraction import WindX
from rex.utilities.execution import SpawnProcessPool
from rex.utilities.loggers import log_mem

logger = logging.getLogger(__name__)


class FlickerRegulations(WindRegulations):
    """Shadow flicker regulation values. """

    def __init__(self, hub_height, rotor_diameter, flicker_threshold=30,
                 regulations_fpath=None):
        """
        Parameters
        ----------
        hub_height : float | int
            Turbine hub height (m).
        rotor_diameter : float | int
            Turbine rotor diameter (m).
        flicker_threshold : float | int, optional
            Maximum number of allowable flicker hours per year.
            By default, `30`.
        regulations_fpath : str, optional
            Path to regulations .csv or .gpkg file. At a minimum, this
            file must contain the following columns: `Feature Type`
            which labels the type of setback that each row represents,
            `Value Type`, which specifies wether the value is a
            multiplier or static height, `Value`, which specifies the
            numeric value of the setback or multiplier, and `FIPS`,
            which specifies a unique 5-digit code for each county (this
            can be an integer - no leading zeros required). Valid
            options for the `Value Type` are:
                - "Hrs/Year"
            If this input is `None`, a generic setback of
            `max_tip_height * multiplier` is used. By default `None`.
        """
        super().__init__(hub_height=hub_height, rotor_diameter=rotor_diameter,
                         regulations_fpath=regulations_fpath,
                         multiplier=1)
        self._base_setback_dist = flicker_threshold

    @property
    def flicker_threshold(self):
        """
        Maximum number of allowable flicker hours per year.

        Returns
        -------
        float
        """
        return self._base_setback_dist

    def _county_regulation_setback(self, county_regulations):
        """Retrieve county regulation setback. """
        setback_type = county_regulations["Value Type"].strip()
        setback = float(county_regulations["Value"])
        if setback_type.lower() != "hrs/year":
            msg = ('Cannot create setback for {}, expecting '
                   '"Hrs/Year", but got {!r}'
                   .format(county_regulations["County"], setback_type))
            logger.warning(msg)
            warn(msg)
            return
        return setback


class TurbineFlicker(AbstractBaseExclusionsMerger):
    """
    Class to compute turbine shadow flicker and exclude sites that will
    cause excessive flicker on building
    """
    STEPS_PER_HOUR = 1
    DEFAULT_FEATURE_OUTFILE = 'flicker.tif'

    def __init__(self, excl_fpath, res_fpath, building_layer, regulations,
                 building_threshold=0, resolution=640, grid_cell_size=90,
                 max_flicker_exclusion_range="10x", tm_dset='techmap_wtk',
                 hsds=False):
        """
        Parameters
        ----------
        excl_fpath : str
            Filepath to exclusions h5 file. File must contain
            "building_layer" and "tm_dset".
        res_fpath : str
            Filepath to wind resource .h5 file containing hourly wind
            direction data
        building_layer : np.ndarray
            Exclusion layer containing buildings from which turbine
            flicker exclusions will be computed.
        regulations : `FlickerRegulations`
            A `FlickerRegulations` object used to shadow flicker
            regulation values.
        building_threshold : float, optional
            Threshold for exclusion layer values to identify pixels with
            buildings, values are % of pixel containing a building. By
            default, `0`.
        resolution : int, optional
            SC resolution, must be input in combination with gid,
            by default 640
        grid_cell_size : float, optional
            Length (m) of a side of each grid cell in `excl_fpath`.
        max_flicker_exclusion_range : float | int | str, optional
            Max distance (m) that flicker exclusions will extend in
            any of the cardinal directions. Can also be a string like
            ``"10x"`` (default), which is interpreted as 10 times the
            turbine rotor diameter. Note that increasing this value can
            lead to drastically instead memory requirements. This value
            may be increased slightly (no more then the size of one grid
            cell) in order to yield odd exclusion array shapes.
        tm_dset : str, optional
            Dataset / layer name for wind toolkit techmap,
            by default 'techmap_wtk'
        hsds : bool, optional
            Boolean flag to use h5pyd to handle .h5 'files' hosted on
            AWS behind HSDS. By default `False`.
        """
        super().__init__(excl_fpath, regulations, hsds)
        self._res_h5 = res_fpath
        self._bld_layer = building_layer
        self._res = resolution
        self._building_threshold = building_threshold
        self._grid_cell_size = grid_cell_size
        self._max_flicker_exclusion_range = (
            self._parse_max_flicker_exclusion_range(
                max_flicker_exclusion_range))
        self._profile = None
        self._flicker_preflight_check(tm_dset=tm_dset)
        self._sc_points = self._get_sc_points(tm_dset=tm_dset)
        self._fips_to_gid = {}

    def _parse_max_flicker_exclusion_range(self, excl_range):
        """Convert max_flicker_exclusion_range to float if necessary. """
        if isinstance(excl_range, str) and excl_range.endswith('x'):
            rd = self._regulations.rotor_diameter
            return float(excl_range.strip('x')) * rd

        if not isinstance(excl_range, (int, float)):
            try:
                excl_range = float(excl_range)
            except Exception as e:
                msg = ('max_flicker_exclusion_range must be numeric but '
                       'received: {}, {}'.format(excl_range, type(excl_range)))
                logger.error(msg)
                raise TypeError(msg) from e

        return excl_range

    def _flicker_preflight_check(self, tm_dset='techmap_wtk'):
        """
        Check to ensure building_layer and tm_dset are in exclusion .h5 file

        Parameters
        ----------
        tm_dset : str, optional
            Dataset / layer name for wind toolkit techmap,
            by default 'techmap_wtk'
        """
        with ExclusionLayers(self._excl_fpath, hsds=self._hsds) as f:
            layers = f.layers
            exclusion_shape = f.shape
            self._profile = f.profile

        if self._bld_layer.shape != exclusion_shape:
            msg = ("Shape of building layer {} does not match shape of "
                   "ExclusionLayers {}"
                   .format(self._bld_layer.shape, exclusion_shape))
            logger.error(msg)
            raise RuntimeError(msg)

        if tm_dset not in layers:
            logger.warning('Could not find techmap "{t}" in {e}. '
                           'Creating {t} using reV TechMapping'
                           .format(t=tm_dset, e=self._excl_fpath))
            try:
                TechMapping.run(self._excl_fpath, self._res_h5,
                                dset=tm_dset)
            except Exception as e:
                logger.exception('TechMapping process failed. Received the '
                                 'following error:\n{}'.format(e))
                raise e

    def _get_sc_points(self, tm_dset='techmap_wtk'):
        """
        Get the valid sc points to run turbine flicker for

        Parameters
        ----------
        tm_dset : str, optional
            Dataset / layer name for wind toolkit techmap, by default
            'techmap_wtk'

        Returns
        -------
        points : pandas.DataFrame
            DataFrame of valid sc point gids with their latitude and longitude
            coordinates and nearest resource gid
        """
        with SupplyCurveExtent(self._excl_fpath, resolution=self._res) as sc:
            points = sc.points
            points['latitude'] = sc.latitude
            points['longitude'] = sc.longitude
            gids = sc.valid_sc_points(tm_dset)
            points = points.loc[gids]

        with WindX(self._res_h5) as f:
            res_gids = f.lat_lon_gid(points[['latitude', 'longitude']].values,
                                     check_lat_lon=False)
            points['res_gid'] = res_gids

        return points

    def _compute_shadow_flicker(self, lat, lon, wind_dir):
        """
        Compute shadow flicker for given location

        Parameters
        ----------
        lat : float
            Latitude coordinate of turbine.
        lon : float
            Longitude coordinate of turbine.
        wind_dir : ndarray
            Time-series of wind direction for turbine.

        Returns
        -------
        shadow_flicker : ndarray
            2D array centered on the turbine with the number of flicker
            hours per "exclusion" pixel
        """
        # Import HOPP dynamically so its not a requirement
        from hybrid.flicker.flicker_mismatch_grid import FlickerMismatch

        rd = self._regulations.rotor_diameter
        self._set_max_grid_size_for_odd_shaped_arr()
        mult = self._max_flicker_exclusion_range / rd
        FlickerMismatch.diam_mult_nwe = mult
        FlickerMismatch.diam_mult_s = mult
        FlickerMismatch.steps_per_hour = self.STEPS_PER_HOUR
        FlickerMismatch.turbine_tower_shadow = False

        assert len(wind_dir) == 8760

        shadow_flicker = FlickerMismatch(lat, lon,
                                         blade_length=rd / 2,
                                         angles_per_step=None,
                                         wind_dir=wind_dir,
                                         gridcell_height=self._grid_cell_size,
                                         gridcell_width=self._grid_cell_size,
                                         gridcells_per_string=1)
        shadow_flicker = shadow_flicker.create_heat_maps(range(0, 8760),
                                                         ("time", ))[0]

        return shadow_flicker

    def _set_max_grid_size_for_odd_shaped_arr(self):
        """Set the max_flicker_exclusion_range to multiple of 0.5 grids """
        mult = np.round(self._max_flicker_exclusion_range
                        / self._grid_cell_size) + 0.5
        self._max_flicker_exclusion_range = mult * self._grid_cell_size

    def _exclude_turbine_flicker(self, point, res_fpath, flicker_threshold):
        """
        Exclude all pixels that will cause flicker exceeding the
        "flicker_threshold" on buildings that exist within
        supply curve point gid subset of "building_layer". Buildings
        are defined as pixels with >= the "building_threshold".
        Shadow flicker is computed at the supply curve point
        resolution and applied to all buildings within that supply curve point
        sub-array. Excluded pixels can extend beyond the supply curve point
        gid subset, for example if a building sits at the edge of the subset.

        Parameters
        ----------
        gid : int
            Supply curve point gid to aggregate wind directions for
        res_fpath : str
            Filepath to wind resource .h5 file containing hourly wind
            direction data
        flicker_threshold : int, optional
            Maximum number of allowable flicker hours, by default 30

        Returns
        -------
        excl_idx : tuple
            (row, col) shifts of pixels to be excluded because they
            will cause excessive shadow flicker from building location
        """

        with WindX(res_fpath, log_vers=False) as f:
            dset = 'winddirection_{}m'.format(self._regulations.hub_height)
            wind_dir = f[dset, :, int(point['res_gid'])]

            # pylint: disable=unsubscriptable-object
            if len(wind_dir) == 8784:
                wind_dir = wind_dir[:-24]

        shadow_flicker = self._compute_shadow_flicker(point['latitude'],
                                                      point['longitude'],
                                                      wind_dir)

        flicker_shifts = _get_flicker_excl_shifts(
            shadow_flicker,flicker_threshold=flicker_threshold)

        return flicker_shifts

    def compute_flicker_exclusions(self, flicker_threshold=30, fips=None,
                                   max_workers=None, out_layer=None,
                                   out_tiff=None):
        """Compute turbine flicker exclusions.

        Exclude all pixels that will cause flicker exceeding the
        "flicker_threshold" on any building in "building_layer".
        Buildings are defined as pixels with >= the "building_threshold
        value in "building_layer". Shadow flicker is computed at the
        supply curve point resolution based on a turbine with
        "hub_height" (m) and applied to all buildings within that supply
        curve point sub-array.

        Parameters
        ----------
        flicker_threshold : int, optional
            Maximum number of allowable flicker hours. By default, `30`.
        fips : int, optional
            If not `None`, only building indices within counties with
            the given FIPS code will be returned. By default, `None`.
        max_workers : int, optional
            Number of workers to use. If 1 run, in serial. If `None`,
            use all available cores. By default, `None`.
        out_layer : str, optional
            Layer to save exclusions under. Layer will be saved in
            `excl_fpath`. By default, `None`.
        out_tiff : str, optional
            Path to output tiff file where exclusions should be saved.
            By default, `None`.

        Returns
        -------
        flicker_arr : ndarray
            2D inclusion array. Pixels to exclude (0) to prevent shadow
            flicker on buildings in "building_layer
        """

        if max_workers is None:
            max_workers = os.cpu_count()

        if fips is None:
            gids = self._sc_points.index
        else:
            gids = self._fips_to_gid.get(fips, [])

        flicker_arr = np.ones(self._bld_layer.shape, dtype=np.uint8)
        if max_workers > 1:
            msg = ('Computing exclusions from {} based on {}m hub height '
                   'turbines with {}m rotor diameters in parallel using {} '
                   'workers'.format(self, self._regulations.hub_height,
                                    self._regulations.rotor_diameter,
                                    max_workers))
            logger.info(msg)

            loggers = [__name__, 'reVX', 'rex']
            with SpawnProcessPool(max_workers=max_workers,
                                  loggers=loggers) as exe:
                futures = {}
                for gid in gids:
                    point = self._sc_points.iloc[gid]

                    # row_idx, col_idx, shape = _get_building_indices(
                    #     self._excl_fpath, self._bld_layer, point.name,
                    #     resolution=self._res, fips=fips,
                    #     building_threshold=self._building_threshold)
                    # if row_idx.size == 0:
                    #     continue

                    future = exe.submit(self._exclude_turbine_flicker,
                                        point, self._res_h5, flicker_threshold)
                    futures[future] = point

                for i, future in enumerate(as_completed(futures)):
                    flicker_shifts = future.result()
                    point = futures[future]

                    row_idx, col_idx = _get_building_indices(
                        self._bld_layer, point.name,
                        resolution=self._res, # fips=fips,
                        building_threshold=self._building_threshold)
                    row_idx, col_idx = _create_excl_indices(
                        (row_idx, col_idx), flicker_shifts,
                        self._bld_layer.shape)

                    flicker_arr[row_idx, col_idx] = 0
                    logger.info('Completed {} out of {} gids'
                                .format((i + 1), len(futures)))
                    log_mem(logger)
        else:
            msg = (
                'Computing exclusions from {} based on {}m hub height, {}m '
                'rotor diameter turbines in serial.'
                .format(self, self._regulations.hub_height,
                        self._regulations.rotor_diameter)
            )
            logger.info(msg)
            for i, gid in enumerate(gids):
                point = self._sc_points.iloc[gid]
            # for i, (_, point) in enumerate(self._sc_points.iterrows()):
                # row_idx, col_idx, shape = _get_building_indices(
                #     self._excl_fpath, self._bld_layer, point.name,
                #     resolution=self._res, fips=fips,
                #     building_threshold=self._building_threshold)
                # if row_idx.size == 0:
                #     continue

                flicker_shifts = self._exclude_turbine_flicker(
                    point, self._res_h5, flicker_threshold)

                row_idx, col_idx = _get_building_indices(
                    self._bld_layer, point.name,
                    resolution=self._res, # fips=fips,
                    building_threshold=self._building_threshold)
                row_idx, col_idx = _create_excl_indices((row_idx, col_idx),
                                                        flicker_shifts,
                                                        self._bld_layer.shape)

                flicker_arr[row_idx, col_idx] = 0
                logger.debug('Completed {} out of {} gids'
                             .format((i + 1), len(self._sc_points)))
                log_mem(logger)

        # if out_layer:
        #     logger.info('Saving flicker inclusion layer to {} as {}'
        #                 .format(self._excl_fpath, out_layer))
        #     description = (
        #         'Pixels with value 0 are excluded as they will cause greater '
        #         'than {} hours of flicker on buildings in {}. Shadow flicker '
        #         'is computed using a {}m hub height, {}m rotor diameter '
        #         'turbine.'
        #         .format(flicker_threshold, self._bld_layer,
        #                 self._regulations.hub_height,
        #                 self._regulations.rotor_diameter)
        #     )
        #     ExclusionsConverter._write_layer(self._excl_fpath, out_layer,
        #                                      self.profile, flicker_arr,
        #                                      description=description)
        # if out_tiff:
        #     logger.info('Saving flicker inclusion layer to {}'
        #                 .format(out_tiff))
        #     ExclusionsConverter._write_geotiff(out_tiff, self.profile,
        #                                        flicker_arr)

        return flicker_arr

    @property
    def profile(self):
        """dict: Geotiff profile. """
        return self._profile

    def pre_process_regulations(self):
        """Reduce regulations to correct state and features. """

        self._fips_to_gid = {}
        reg_fips = self._regulations.FIPS.unique()
        with SupplyCurveExtent(self._excl_fpath, resolution=self._res) as sc:
            for gid in self._sc_points.index:
                for fips in np.unique(sc.get_excl_points('cnty_fips', gid)):
                    if fips in reg_fips:
                        self._fips_to_gid.setdefault(fips, []).append(gid)

        # TODO: Turn this into a warning
        assert len(self._fips_to_gid) == len(reg_fips), "Some FIPS not found"

    def compute_local_exclusions(self, regulation_value, cnty):
        """Compute local flicker exclusions.

        This method computes a flicker exclusion layer using the
        information about the input county.

        Parameters
        ----------
        regulation_value : float | int
            Maximum number of allowable flicker hours in county.
        cnty : geopandas.GeoDataFrame
            Regulations for a single county.

        Returns
        -------
        flicker : ndarray
            Raster array of flicker exclusions
        """
        cnty_fips = cnty.iloc[0]['FIPS']
        logger.debug('- Computing flicker for county FIPS {}'
                     .format(cnty_fips))
        return self.compute_flicker_exclusions(
            flicker_threshold=regulation_value, fips=cnty_fips, max_workers=1)

    def compute_generic_exclusions(self, max_workers=None):
        """Compute generic flicker exclusions.

        This method will compute a generic flicker exclusion layer.

        Parameters
        ----------
        max_workers : int, optional
            Number of workers to use for exclusions computation, if 1
            run in serial, if > 1 run in parallel with that many
            workers, if `None` run in parallel on all available cores.
            By default `None`.

        Returns
        -------
        flicker : ndarray
            Raster array of flicker exclusions
        """
        logger.info('Computing generic flicker exclusions...')
        return self.compute_flicker_exclusions(flicker_threshold=30,
                                               fips=None,
                                               max_workers=max_workers)

    def input_output_filenames(self, out_dir, features_fpath):
        """Generate pairs of input/output file names.

        Parameters
        ----------
        out_dir : str
            Path to output file directory.
        features_fpath : : str
            Path to features file. This path can contain
            any pattern that can be used in the glob function.
            For example, `/path/to/features/[A]*` would match
            with all the features in the directory
            `/path/to/features/` that start with "A". This input
            can also be a directory, but that directory must ONLY
            contain feature files. If your feature files are mixed
            with other files or directories, use something like
            `/path/to/features/*.geojson`.

        Yields
        ------
        tuple
            An input-output filename pair.
        """
        for fpath in [self.DEFAULT_FEATURE_OUTFILE]:
            fn = flicker_fn_out(self._regulations.hub_height,
                                self._regulations.rotor_diameter)
            geotiff = ".".join(fn.split('.')[:-1] + ['tif'])
            yield fpath, os.path.join(out_dir, geotiff)



    # @classmethod
    # def run(cls, excl_fpath, res_fpath, building_layer, tm_dset='techmap_wtk',
    #         building_threshold=0, flicker_threshold=30, resolution=640,
    #         grid_cell_size=90, max_flicker_exclusion_range=10_000,
    #         max_workers=None, out_layer=None, out_tiff=None):
    #     """Run flicker exclusion layer generation.

    #     Exclude all pixels that will cause flicker exceeding the
    #     "flicker_threshold" on any building in "building_layer".
    #     Buildings are defined as pixels with >= the "building_threshold
    #     value in "building_layer". Shadow flicker is computed at the
    #     supply curve point resolution based on a turbine with
    #     "hub_height" (m) and applied to all buildings within that supply
    #     curve point sub-array.

    #     Parameters
    #     ----------
    #     excl_fpath : str
    #         Filepath to exclusions h5 file. File must contain
    #         `building_layer` and `tm_dset`.
    #     res_fpath : str
    #         Filepath to wind resource .h5 file containing hourly wind
    #         direction data.
    #     building_layer : str
    #         Exclusion layer containing buildings from which turbine
    #         flicker exclusions will be computed.
    #     regulations : `FlickerRegulations`
    #         A `FlickerRegulations` object used to shadow flicker
    #         regulation values.
    #     tm_dset : str, optional
    #         Dataset / layer name for wind toolkit techmap. By default,
    #         `'techmap_wtk'`.
    #     building_threshold : float, optional
    #         Threshold for exclusion layer values to identify pixels with
    #         buildings, values are % of pixel containing a building. By
    #         default, `0`.
    #     flicker_threshold : int, optional
    #         Maximum number of allowable flicker hours. By default, `30`.
    #     resolution : int, optional
    #         SC resolution, must be input in combination with gid.
    #         By default, `640`.
    #     grid_cell_size : float, optional
    #         Length (m) of a side of each grid cell in `excl_fpath`.
    #     max_flicker_exclusion_range : float, optional
    #         Max distance (m) that flicker exclusions will extend in
    #         any of the cardinal directions. Note that increasing this
    #         value can lead to drastically instead memory requirements.
    #         This value may be increased slightly in order to yield
    #         odd exclusion array shapes.
    #     max_workers : int, optional
    #         Number of workers to use. If 1 run, in serial. If `None`,
    #         use all available cores. By default, `None`.
    #     out_layer : str, optional
    #         Layer to save exclusions under. Layer will be saved in
    #         `excl_fpath`. By default, `None`.
    #     out_tiff : str, optional
    #         Path to output tiff file where exclusions should be saved.
    #         By default, `None`.

    #     Returns
    #     -------
    #     flicker_arr : ndarray
    #         2D inclusion array. Pixels to exclude (0) to prevent shadow
    #         flicker on buildings in "building_layer"
    #     """
    #     flicker = cls(excl_fpath, res_fpath, building_layer,
    #                   resolution=resolution, grid_cell_size=grid_cell_size,
    #                   max_flicker_exclusion_range=max_flicker_exclusion_range,
    #                   tm_dset=tm_dset)
    #     out_excl = flicker.compute_exclusions(
    #         building_threshold=building_threshold,
    #         flicker_threshold=flicker_threshold,
    #         max_workers=max_workers,
    #         out_layer=out_layer,
    #         out_tiff=out_tiff
    #     )
    #     return out_excl


def _get_building_indices(building_layer, gid, resolution=640,
                          building_threshold=0,
                          fips=None, fips_layer="cnty_fips", hsds=False):
    """Find buildings exclusion indices

    Parameters
    ----------
    building_layer : np.ndarray
        Exclusion layer containing buildings from which turbine flicker
        exclusions will be computed.
    gid : int
        SC point gid to extract buildings for.
    resolution : int, optional
        SC resolution, must be input in combination with gid.
        By default, `640`.
    building_threshold : float, optional
        Threshold for exclusion layer values to identify pixels with
        buildings, values are % of pixel containing a building.
        By default, `0`.
    fips : int, optional
        If not `None`, only building indices within counties with the
        given FIPS code will be returned. By default, `None`.
    fips_layer : str, optional
        Name of fips layer in `excl_fpath`. By default, `"cnty_fips"`.
    hsds : bool, optional
            Boolean flag to use h5pyd to handle .h5 'files' hosted on
            AWS behind HSDS. By default `False`.

    Returns
    -------
    row_idx : ndarray
        Axis 0 indices of building in sc point sub-array in full
        exclusion array.
    col_idx : ndarray
        Axis 1 indices of building in sc point sub-array in full
        exclusion array.
    shape : tuple
        Full exclusion array shape.
    """
    row_slice, col_slice = MeanWindDirectionsPoint.get_agg_slices(
        gid, building_layer.shape, resolution
    )
    sc_blds = building_layer[row_slice, col_slice]
    row_idx = np.array(range(*row_slice.indices(row_slice.stop)))
    col_idx = np.array(range(*col_slice.indices(col_slice.stop)))

    bld_row_idx, bld_col_idx = np.where((sc_blds > building_threshold)
                                        # & (fips_vals == fips
                                        #    if fips is not None else True)
                                        )

    return row_idx[bld_row_idx], col_idx[bld_col_idx]


def _create_excl_indices(bld_idx, flicker_shifts, shape):
    """
    Create 2D (row, col) indices of pixels to be excluded based on
    building indices and shadow flicker shifts.

    Parameters
    ----------
    bld_idx : tuple
        (row, col) indices of building onto which shadow flicker
        exclusions are to be mapped.
    flicker_shifts : tuple
        Index shifts (row, col) from building locations to exclude based
        on shadow flicker results. Shifts are based on shadow flicker
        threshold. Shadow flicker array is inverted to represent mapping
        of shadow onto buildings
    shape : tuple
        Full exclusion array shape

    Returns
    -------
    excl_row_idx : ndarray
        Row (axis 0) indices of pixels to be excluded because they will
        cause excessive shadow flicker on building in supply curve point
        gid subset
    excl_col_idx : ndarray
        Column (axis 1) indices of pixels to be excluded because they
        will cause excessive shadow flicker on building in supply curve
        point gid subset
    """
    row_idx, col_idx = bld_idx
    row_shifts, col_shifts = flicker_shifts

    excl_row_idx = (row_idx + row_shifts[:, None]).ravel()
    excl_row_idx[excl_row_idx < 0] = 0
    excl_row_idx[excl_row_idx >= shape[0]] = shape[0] - 1

    excl_col_idx = (col_idx + col_shifts[:, None]).ravel()
    excl_col_idx[excl_col_idx < 0] = 0
    excl_col_idx[excl_col_idx >= shape[1]] = shape[1] - 1

    return excl_row_idx.astype(np.int32), excl_col_idx.astype(np.int32)


def _invert_shadow_flicker_arr(shadow_flicker):
    """
    Check to ensure the shadow_flicker array is odd in shape, i.e. both
    dimensions are odd allowing for a central pixel for the turbine to
    sit on. Flip both axes to mimic the turbine sitting on each
    building. All flicker pixels will now indicate locations where a
    turbine would need to be to cause flicker on said building

    Parameters
    ----------
    shadow_flicker : ndarray
        2D array centered on the turbine with the number of flicker
        hours per "exclusion" pixel

    Returns
    -------
    shadow_flicker : ndarray
        Inverted 2D shadow flicker array with odd dimensions if needed.
    """
    reduce_slice = ()
    reduce_arr = False
    for s in shadow_flicker.shape:
        if s % 2:
            reduce_slice += (slice(None), )
        else:
            reduce_slice += (slice(0, -1), )
            reduce_arr = True

    if reduce_arr:
        shape_in = shadow_flicker.shape
        shadow_flicker = shadow_flicker[reduce_slice]
        msg = ('Shadow flicker array with shape {} does not have a '
               'central pixel! Shade has been reduced to {}!'
               .format(shape_in, shadow_flicker.shape))
        logger.warning(msg)
        warn(msg)

    return shadow_flicker[::-1, ::-1]


def _get_flicker_excl_shifts(shadow_flicker, flicker_threshold=30):
    """
    Determine locations of shadow flicker that exceed the given threshold,
    convert to row and column shifts. These are the locations turbines
    would need to in relation to building to cause flicker exceeding the
    threshold value.

    Parameters
    ----------
    shadow_flicker : ndarray
        2D array centered on the turbine with the number of flicker hours
        per "exclusion" pixel
    flicker_threshold : int, optional
        Maximum number of allowable flicker hours, by default 30

    Returns
    -------
    row_shifts : ndarray
        Shifts along axis 0 from building location to pixels to be excluded
    col_shifts : ndarray
        Shifts along axis 1 from building location to pixels to be excluded
    """
    # ensure shadow_flicker array is regularly shaped and invert for
    # mapping to buildings
    shadow_flicker = _invert_shadow_flicker_arr(shadow_flicker)

    # normalize by number of time-steps to match shadow flicker results
    flicker_threshold /= 8760
    shape = shadow_flicker.shape
    row_shifts, col_shifts = np.where(shadow_flicker > flicker_threshold)
    check = (np.any(np.isin(row_shifts, [0, shape[0] - 1]))
             or np.any(np.isin(col_shifts, [0, shape[1] - 1])))
    if check:
        msg = ("Turbine flicker appears to extend beyond the FlickerModel "
               "domain! Consider increasing the maximum flicker exclusion "
               "range.")
        logger.warning(msg)
        warn(msg)

    row_shifts -= shape[0] // 2
    col_shifts -= shape[1] // 2

    return row_shifts, col_shifts


def load_building_layer(excl_fpath, building_layer=None, features_path=None,
                        hsds=False):
    """Load building data from excl .h5 file or geotiff.

    Parameters
    ----------
    excl_fpath : str
        Filepath to exclusions h5 file. If `building_layer` is not
        `None`, this file must contain `building_layer`.
    building_layer : float | int
        Exclusion layer containing buildings from which turbine
        flicker exclusions will be computed.  By default, `None`.
    features_path : str | int
        Path to input tif  containing buildings from which turbine
        flicker exclusions will be computed. By default, `None`.
    hsds : bool, optional
        Boolean flag to use h5pyd to handle .h5 'files' hosted on AWS
        behind HSDS. By default `False`.

    Returns
    -------
    np.ndarray
        Array containing building data.

    Raises
    ------
    RuntimeError
        If not enough info is provided (all inputs are `None`), or too
        much info is given (all inputs are not `None`).
    RuntimeError
        If `building_layer` is not None but also does not exist in
        `excl_fpath` .h5 file.
    """

    if building_layer is not None:
        with ExclusionLayers(excl_fpath, hsds=hsds) as f:
            if building_layer not in f.layers:
                msg = ("{} is not available in {}"
                       .format(building_layer, excl_fpath))
                logger.error(msg)
                raise RuntimeError(msg)
            logger.debug("Loading building data from {}, layer {}"
                         .format(excl_fpath, building_layer))
            return f[building_layer]

    if features_path is not None:
        logger.debug("Loading building data from {}".format(features_path))
        with Geotiff(features_path) as f:
            return f.values[0]

    raise RuntimeError("Must provide either `features_path` or "
                        "`building_layer` (but not both).")


def flicker_fn_out(hub_height, rotor_diameter):
    """Generate flicker tiff outfile name.

    Parameters
    ----------
    hub_height : int
        Turbine hub-height (m).
    rotor_diameter : int
        Turbine rotor diameter (m).

    Returns
    -------
    str
        Name of flicker outfile.
    """
    return "flicker_{}hh_{}rd.tif".format(hub_height, rotor_diameter)
