# -*- coding: utf-8 -*-
"""
Turbine Flicker exclusions calculator
"""
from concurrent.futures import as_completed
from hybrid.flicker.flicker_mismatch_grid import FlickerMismatch
import logging
import numpy as np
import os
from warnings import warn

from reV.handlers.exclusions import ExclusionLayers
from reV.supply_curve.points import SupplyCurveExtent
from reV.supply_curve.tech_mapping import TechMapping
from reVX.wind_dirs.mean_wind_dirs_point import MeanWindDirectionsPoint
from reVX.utilities.exclusions_converter import ExclusionsConverter
from rex.utilities.execution import SpawnProcessPool

logger = logging.getLogger(__name__)


class TurbineFlicker:
    """
    Class to compute turbine shadow flicker and exclude sites that will
    cause excessive flicker on building
    """
    STEPS_PER_HOUR = 1
    GRIDCELL_SIZE = 90
    FLICKER_ARRAY_LEN = 65

    def __init__(self, excl_fpath, res_fpath, building_layer,
                 tm_dset='techmap_wtk'):
        """
        Parameters
        ----------
        excl_fpath : str
            Filepath to exclusions h5 file. File must contain "building_layer"
            and "tm_dset".
        res_fpath : str
            Filepath to wind resource .h5 file containing hourly wind
            direction data
        building_layer : str
            Exclusion layer containing buildings from which turbine flicker
            exclusions will be computed.
        tm_dset : str, optional
            Dataset / layer name for wind toolkit techmap,
            by default 'techmap_wtk'
        """
        self._excl_h5 = excl_fpath
        self._res_h5 = res_fpath
        self._bld_layer = building_layer
        self._tm_dset = tm_dset
        self._preflight_check()

    def __repr__(self):
        msg = ("{} from {}"
               .format(self.__class__.__name__, self._bld_layer))

        return msg

    @staticmethod
    def _aggregate_wind_dirs(gid, excl_fpath, res_fpath, hub_height,
                             tm_dset='techmap_wtk', resolution=128,
                             exclusion_shape=None):
        """
        Compute the mean wind direction profile (time-series) for the given
        supply curve point gid at the desired hub-height

        Parameters
        ----------
        gid : int
            Supply curve point gid to aggregate wind directions for
        excl_fpath : str
            Filepath to exclusions h5 file. File must contain "tm_dset".
        res_fpath : str
            Filepath to wind resource .h5 file containing hourly wind
            direction data
        hub_height : int
            Hub-height in meters to compute turbine shadow flicker for
        tm_dset : str, optional
            Dataset / layer name for wind toolkit techmap,
            by default 'techmap_wtk'
        resolution : int, optional
            SC resolution, must be input in combination with gid,
            by default 128
        exclusion_shape : tuple, optional
            Shape of the full exclusions extent (rows, cols). Inputing this
            will speed things up considerably. by default None

        Returns
        -------
        site_meta : pandas.Series
            Meta data for supply curve point
        wind_dir : ndarray
            Hourly time-series of aggregated mean wind direction for desired
            supply curve point gid and hub-height
        """
        wind_dir_dset = 'winddirection_{}m'.format(hub_height)
        out = MeanWindDirectionsPoint.run(
            gid,
            excl_fpath,
            res_fpath,
            tm_dset,
            wind_dir_dset,
            resolution=resolution,
            exclusion_shape=exclusion_shape)

        meta = out['meta']
        wind_dir = out[wind_dir_dset]

        # Drop last day of leap years
        if len(wind_dir) == 8784:
            wind_dir = wind_dir[:-24]

        return meta, wind_dir

    @classmethod
    def _compute_shadow_flicker(cls, lat, lon, blade_length, wind_dir):
        """
        Compute shadow flicker for given location

        Parameters
        ----------
        lat : float
            Latitude coordinate of turbine
        lon : float
            Longitude coordinate of turbine
        blade_length : float
            Turbine blade length. Hub height = 2.5 * blade length
        wind_dir : ndarray
            Time-series of wind direction for turbine

        Returns
        -------
        shadow_flicker : ndarray
            2D array centered on the turbine with the number of flicker hours
            per "exclusion" pixel
        """
        mult = (cls.FLICKER_ARRAY_LEN * cls.GRIDCELL_SIZE) / 2
        mult = mult / (blade_length * 2)
        FlickerMismatch.diam_mult_nwe = mult
        FlickerMismatch.diam_mult_s = mult
        FlickerMismatch.steps_per_hour = cls.STEPS_PER_HOUR
        FlickerMismatch.turbine_tower_shadow = False

        assert len(wind_dir) == 8760

        shadow_flicker = FlickerMismatch(lat, lon,
                                         blade_length=blade_length,
                                         angles_per_step=None,
                                         wind_dir=wind_dir,
                                         gridcell_height=cls.GRIDCELL_SIZE,
                                         gridcell_width=cls.GRIDCELL_SIZE,
                                         gridcells_per_string=1)
        shadow_flicker = shadow_flicker.create_heat_maps(range(0, 8760),
                                                         ("time", ))[0]

        return shadow_flicker

    @staticmethod
    def _check_shadow_flicker_arr(shadow_flicker):
        """
        Check to ensure the shadow_flicker array is odd in shape, i.e. both
        dimensions are odd allowing for a central pixel for the turbine to
        sit on. Flip 0-axis to mimic the turbine sitting on each building.
        All flicker pixels will now indicate locations where a turbine would
        need to be to cause flicker on said building

        Parameters
        ----------
        shadow_flicker : ndarray
            2D array centered on the turbine with the number of flicker hours
            per "exclusion" pixel

        Returns
        -------
        shadow_flicker : ndarray
            Updated 2D shadow flicker array with odd dimensions if needed
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

        return shadow_flicker[::-1]

    @classmethod
    def _threshold_flicker(cls, shadow_flicker, flicker_threshold=30):
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
        # ensure shadow_flicker array is regularly shaped
        shadow_flicker = cls._check_shadow_flicker_arr(shadow_flicker)

        # normalize by number of time-steps to match shadow flicker results
        flicker_threshold /= 8760
        shape = shadow_flicker.shape
        row_shifts, col_shifts = np.where(shadow_flicker > flicker_threshold)
        check = (np.any(np.isin(row_shifts, [0, shape[0] - 1]))
                 or np.any(np.isin(col_shifts, [0, shape[1] - 1])))
        if check:
            msg = ("Turbine flicker exceeding {} appears to extend beyond the "
                   "FlickerModel domain! Please increase the "
                   "FLICKER_ARRAY_LEN and try again!")
            logger.error(msg)
            raise RuntimeError(msg)

        row_shifts -= shape[0] // 2
        col_shifts -= shape[1] // 2

        return row_shifts, col_shifts

    @staticmethod
    def _get_building_indices(excl_fpath, building_layer, gid,
                              resolution=128, building_threshold=0):
        """
        Find buildings in sc point sub-array and convert indices to full
        exclusion indices

        Parameters
        ----------
        excl_fpath : str
            Filepath to exclusions h5 file. File must contain "building_layer"
            and "tm_dset".
        building_layer : str
            Exclusion layer containing buildings from which turbine flicker
            exclusions will be computed.
        gid : int
            sc point gid to extract buildings for
        resolution : int, optional
            SC resolution, must be input in combination with gid,
            by default 128
        building_threshold : float, optional
            Threshold for exclusion layer values to identify pixels with
            buildings, values are % of pixel containing a building,
            by default 0

        Returns
        -------
        row_idx : ndarray
            Axis 0 indices of building in sc point sub-array in full exclusion
            array
        col_idx : ndarray
            Axis 1 indices of building in sc point sub-array in full exclusion
            array
        shape : tuple
            Exclusion shape
        """
        with ExclusionLayers(excl_fpath) as f:
            shape = f.shape
            row_slice, col_slice = MeanWindDirectionsPoint.get_agg_slices(
                gid, shape, resolution)

            sc_blds = f[building_layer, row_slice, col_slice]

        row_idx = np.array(range(*row_slice.indices(row_slice.stop)))
        col_idx = np.array(range(*col_slice.indices(col_slice.stop)))
        bld_row_idx, bld_col_idx = np.where(sc_blds > building_threshold)

        return row_idx[bld_row_idx], col_idx[bld_col_idx], shape

    @classmethod
    def _exclude_turbine_flicker(cls, gid, excl_fpath, res_fpath,
                                 building_layer, hub_height,
                                 building_threshold=0, flicker_threshold=30,
                                 tm_dset='techmap_wtk', resolution=128):
        """
        Exclude all pixels that will cause flicker exceeding the
        "flicker_threshold" on any building in "building_layer". Buildings
        are defined as pixels with >= the "building_threshold value in
        "building_layer". Shadow flicker is computed at the supply curve point
        resolution and applied to all buildings within that supply curve point
        sub-array.

        Parameters
        ----------
        gid : int
            Supply curve point gid to aggregate wind directions for
        excl_fpath : str
            Filepath to exclusions h5 file. File must contain "tm_dset".
        res_fpath : str
            Filepath to wind resource .h5 file containing hourly wind
            direction data
        building_layer : str
            Exclusion layer containing buildings from which turbine flicker
            exclusions will be computed.
        hub_height : int
            Hub-height in meters to compute turbine shadow flicker for
        building_threshold : float, optional
            Threshold for exclusion layer values to identify pixels with
            buildings, values are % of pixel containing a building,
            by default 0
        flicker_threshold : int, optional
            Maximum number of allowable flicker hours, by default 30
        tm_dset : str, optional
            Dataset / layer name for wind toolkit techmap,
            by default 'techmap_wtk'
        resolution : int, optional
            SC resolution, must be input in combination with gid,
            by default 128

        Returns
        -------
        excl_row_idx : ndarray
            Axis 0 indices of pixels to be excluded
        excl_col_idx : ndarray
            Axis 1 indices of pixels to be excluded
        """
        row_idx, col_idx, shape = cls._get_building_indices(
            excl_fpath, building_layer, gid,
            resolution=resolution, building_threshold=building_threshold)

        meta, wind_dir = cls._aggregate_wind_dirs(gid,
                                                  excl_fpath,
                                                  res_fpath,
                                                  hub_height,
                                                  tm_dset=tm_dset,
                                                  resolution=resolution,
                                                  exclusion_shape=shape)
        blade_length = hub_height / 2.5
        shadow_flicker = cls._compute_shadow_flicker(meta['latitude'],
                                                     meta['longitude'],
                                                     blade_length,
                                                     wind_dir)

        row_shifts, col_shifts = cls._threshold_flicker(
            shadow_flicker, flicker_threshold=flicker_threshold)

        excl_row_idx = (row_idx + row_shifts[:, None]).ravel()
        excl_row_idx[excl_row_idx < 0] = 0
        excl_row_idx[excl_row_idx >= shape[0]] = shape[0] - 1

        excl_col_idx = (col_idx + col_shifts[:, None]).ravel()
        excl_col_idx[excl_col_idx < 0] = 0
        excl_col_idx[excl_col_idx >= shape[1]] = shape[1] - 1

        return excl_row_idx, excl_col_idx

    def _preflight_check(self):
        """
        Check to ensure building_layer and tm_dset are in exclusion .h5 file
        """
        with ExclusionLayers(self._excl_h5) as f:
            layers = f.layers

        if self._bld_layer not in layers:
            msg = ("{} is not available in {}"
                   .format(self._bld_layer, self._excl_h5))
            logger.error(msg)
            raise RuntimeError(msg)

        if self._tm_dset not in layers:
            logger.warning('Could not find techmap "{}" in {}. '
                           'Creating {} using reV TechMapping'
                           .format(self._tm_dset, self._excl_h5,
                                   self._tm_dset))
            try:
                TechMapping.run(self._excl_h5, self._res_h5,
                                dset=self._tm_dset)
            except Exception as e:
                logger.exception('TechMapping process failed. Received the '
                                 'following error:\n{}'.format(e))
                raise e

    def compute_exclusions(self, hub_height, building_threshold=0,
                           flicker_threshold=30, resolution=128,
                           max_workers=None, out_layer=None):
        """
        Exclude all pixels that will cause flicker exceeding the
        "flicker_threshold" on any building in "building_layer". Buildings
        are defined as pixels with >= the "building_threshold value in
        "building_layer". Shadow flicker is computed at the supply curve point
        resolution based on a turbine with "hub_height" (m) and applied to all
        buildings within that supply curve point sub-array.

        Parameters
        ----------
        hub_height : int
            Hub-height in meters to compute turbine shadow flicker for
        building_threshold : float, optional
            Threshold for exclusion layer values to identify pixels with
            buildings, values are % of pixel containing a building,
            by default 0
        flicker_threshold : int, optional
            Maximum number of allowable flicker hours, by default 30
        resolution : int, optional
            SC resolution, must be input in combination with gid,
            by default 128
        max_workers : None | int, optional
            Number of workers to use, if 1 run in serial, if None use all
            available cores, by default None
        out_layer : str, optional
            Layer to save exclusions under. Layer will be saved in
            "excl_fpath", by default None

        Returns
        -------
        flicker_excl : ndarray
            2D array of pixels to exclude to prevent shadow flicker on
            buildings in "building_layer"
        """
        with SupplyCurveExtent(self._excl_h5, resolution=resolution) as sc:
            exclusion_shape = sc.exclusions.shape
            profile = sc.exclusions.profile
            gids = sc.valid_sc_points(self._tm_dset)

        if max_workers is None:
            max_workers = os.cpu_count()

        etf_kwargs = {"building_threshold": building_threshold,
                      "flicker_threshold": flicker_threshold,
                      "tm_dset": self._tm_dset,
                      "resolution": resolution}
        flicker_excl = np.zeros(exclusion_shape, dtype=np.int8)
        if max_workers > 1:
            msg = ('Computing exclusions from {} based on {}m turbines '
                   'in parallel using {} workers'
                   .format(self, hub_height, max_workers))
            logger.info(msg)

            loggers = [__name__, 'reVX', 'rex']
            with SpawnProcessPool(max_workers=max_workers,
                                  loggers=loggers) as exe:
                futures = []
                for gid in gids:
                    future = exe.submit(self._exclude_turbine_flicker,
                                        gid, self._excl_h5, self._res_h5,
                                        self._bld_layer, hub_height,
                                        **etf_kwargs)
                    futures.append(future)

                row_idx = []
                col_idx = []
                for i, future in enumerate(as_completed(futures)):
                    gid_row_idx, gid_col_idx = future.result()
                    row_idx.extend(gid_row_idx)
                    col_idx.extend(gid_col_idx)
                    logger.debug('Completed {} out of {} gids'
                                 .format((i + 1), len(gids)))
        else:
            msg = ('Computing exclusions from {} based on {}m turbines in '
                   'serial'.format(self, hub_height))
            logger.info(msg)
            row_idx = []
            col_idx = []
            for i, gid in enumerate(gids):
                gid_row_idx, gid_col_idx = self._exclude_turbine_flicker(
                    gid, self._excl_h5, self._res_h5, self._bld_layer,
                    hub_height, **etf_kwargs)
                row_idx.extend(gid_row_idx)
                col_idx.extend(gid_col_idx)
                logger.debug('Completed {} out of {} gids'
                             .format((i + 1), len(gids)))

        flicker_excl[row_idx, col_idx] = 1

        if out_layer:
            logger.info('Saving flicker exclusions to {} as {}'
                        .format(self._excl_h5, out_layer))
            description = ("Pixels with value 1 will cause greater than {} "
                           "hours of flicker on buildings in {}. Shadow "
                           "flicker is computed using a {}m turbine."
                           .format(flicker_threshold, self._bld_layer,
                                   hub_height))
            ExclusionsConverter._write_layer(self._excl_h5, out_layer,
                                             profile, flicker_excl,
                                             description=description)

        return flicker_excl

    @classmethod
    def run(cls, excl_fpath, res_fpath, building_layer, hub_height,
            tm_dset='techmap_wtk', building_threshold=0,
            flicker_threshold=30, resolution=128,
            max_workers=None, out_layer=None):
        """
        Exclude all pixels that will cause flicker exceeding the
        "flicker_threshold" on any building in "building_layer". Buildings
        are defined as pixels with >= the "building_threshold value in
        "building_layer". Shadow flicker is computed at the supply curve point
        resolution based on a turbine with "hub_height" (m) and applied to all
        buildings within that supply curve point sub-array.

        Parameters
        ----------
        excl_fpath : str
            Filepath to exclusions h5 file. File must contain "building_layer"
            and "tm_dset".
        res_fpath : str
            Filepath to wind resource .h5 file containing hourly wind
            direction data
        building_layer : str
            Exclusion layer containing buildings from which turbine flicker
            exclusions will be computed.
        hub_height : int
            Hub-height in meters to compute turbine shadow flicker for
        tm_dset : str, optional
            Dataset / layer name for wind toolkit techmap,
            by default 'techmap_wtk'
        building_threshold : float, optional
            Threshold for exclusion layer values to identify pixels with
            buildings, values are % of pixel containing a building,
            by default 0
        flicker_threshold : int, optional
            Maximum number of allowable flicker hours, by default 30
        resolution : int, optional
            SC resolution, must be input in combination with gid,
            by default 128
        max_workers : None | int, optional
            Number of workers to use, if 1 run in serial, if None use all
            available cores, by default None
        out_layer : str, optional
            Layer to save exclusions under. Layer will be saved in
            "excl_fpath", by default None

        Returns
        -------
        flicker_excl : ndarray
            2D array of pixels to exclude to prevent shadow flicker on
            buildings in "building_layer"
        """
        flicker = cls(excl_fpath, res_fpath, building_layer,
                      tm_dset=tm_dset)
        out_excl = flicker.compute_exclusions(
            hub_height,
            building_threshold=building_threshold,
            flicker_threshold=flicker_threshold,
            resolution=resolution,
            max_workers=max_workers,
            out_layer=out_layer)

        return out_excl
