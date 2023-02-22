# -*- coding: utf-8 -*-
"""
Compute setbacks exclusions
"""
import os
import logging
import pathlib
from copy import deepcopy
from warnings import warn
from math import floor, ceil
from itertools import product
from abc import abstractmethod
from concurrent.futures import as_completed

import numpy as np
import geopandas as gpd
from rasterio import windows, transform, Affine, features as rio_features

from rex.utilities import log_mem
from rex.utilities.execution import SpawnProcessPool
from reV.handlers.exclusions import ExclusionLayers
from reVX.handlers.geopackage import GPKGMeta
from reVX.utilities.exclusions import AbstractBaseExclusionsMerger

logger = logging.getLogger(__name__)


def features_with_centroid_in_county(features, county):
    """Find features with centroids within the given county.

    Parameters
    ----------
    features : geopandas.GeoDataFrame
        Features to setback from.
    county : geopandas.GeoDataFrame
        Regulations for a single county.

    Returns
    -------
    features : geopandas.GeoDataFrame
        Features that have centroid in county.
    """

    mask = features.centroid.within(county['geometry'].values[0])
    return features.loc[mask]


def features_clipped_to_county(features, county):
    """Clip features to the given county geometry.

    Parameters
    ----------
    features : geopandas.GeoDataFrame
        Features to setback from.
    county : geopandas.GeoDataFrame
        Regulations for a single county.

    Returns
    -------
    features : geopandas.GeoDataFrame
        Features clipped to county geometry.
    """
    tmp = gpd.clip(features, county)
    return tmp[~tmp.is_empty]


class Rasterizer:
    """Helper class to rasterize shapes."""

    def __init__(self, excl_fpath, weights_calculation_upscale_factor,
                 hsds=False):
        """
        Parameters
        ----------
        excl_fpath : str
            Path to .h5 file containing template layers. The raster will
            match the shape and profile of these layers.
        weights_calculation_upscale_factor : int
            If this value is an int > 1, the output will be a layer with
            **inclusion** weight values (floats ranging from 0 to 1).
            Note that this is backwards w.r.t the typical output of
            exclusion integer values (1 for excluded, 0 otherwise).
            Values <= 1 will still return a standard exclusion mask.
            For example, a cell that was previously excluded with a
            a boolean mask (value of 1) may instead be converted to an
            inclusion weight value of 0.75, meaning that 75% of the area
            corresponding to that point should be included (i.e. the
            exclusion feature only intersected a small portion - 25% -
            of the cell). This percentage inclusion value is calculated
            by upscaling the output array using this input value,
            rasterizing the exclusion features onto it, and counting the
            number of resulting sub-cells excluded by the feature. For
            example, setting the value to `3` would split each output
            cell into nine sub-cells - 3 divisions in each dimension.
            After the feature is rasterized on this high-resolution
            sub-grid, the area of the non-excluded sub-cells is totaled
            and divided by the area of the original cell to obtain the
            final inclusion percentage. Therefore, a larger upscale
            factor results in more accurate percentage values. However,
            this process is memory intensive and scales quadratically
            with the upscale factor. A good way to estimate your minimum
            memory requirement is to use the following formula:

            .. math:: memory (GB) = s_0 * s_1 * (sf^2 * 2 + 4) / 1073741824,

            where :math:`s_0` and :math:`s_1` are the dimensions (shape)
            of your exclusion layer and :math:`sf` is the scale factor
            (be sure to add several GB for any other overhead required
            by the rest of the process). If `None` (or a value <= 1),
            this process is skipped and the output is a boolean
            exclusion mask. By default `None`.
        """
        props = _parse_excl_properties(excl_fpath, hsds=hsds)
        self._shape, self._profile = props
        self._scale_factor = weights_calculation_upscale_factor or 1
        self._scale_factor = int(self._scale_factor // 1)

    @property
    def profile(self):
        """Geotiff profile.

        Returns
        -------
        dict
        """
        return self._profile

    @property
    def transform(self):
        """rasterio.Affine: Affine transform for exclusion layer. """
        return Affine(*self.profile["transform"])

    @property
    def arr_shape(self):
        """Rasterize array shape.

        Returns
        -------
        tuple
        """
        return self._shape

    @property
    def inclusions(self):
        """Flag indicating wether or not the output raster represents
        inclusion values.

        Returns
        -------
        bool
        """
        return self._scale_factor > 1

    def _no_exclusions_array(self, multiplier=1, window=None):
        """Get an array of the correct shape representing no exclusions.

        The array contains all zeros, and a new one is created
        for every function call.

        Parameters
        ----------
        multiplier : int, optional
            Integer multiplier value used to scale up the dimensions of
            the array exclusions array (e.g. multiplier of 3 turns an
            array of shape (10, 20) into an array of shape (30, 60)).
        window : :cls:`rasterio.windows.Window`
            A ``rasterio`` window defining the area of the raster. Can
            be used to speed up computation and decrease memory
            requirements if features are localized to a small portion of
            the raster array.

        Returns
        -------
        np.array
            Array of zeros representing no exclusions.
        """
        if window is None:
            shape = tuple(x * multiplier for x in self.arr_shape[1:])
        else:
            shape = (window.height * multiplier, window.width * multiplier)
        return np.zeros(shape, dtype='uint8')

    def rasterize(self, shapes, window=None):
        """Convert geometries into exclusions array.

        Parameters
        ----------
        shapes : list, optional
            List of geometries to rasterize (i.e. list(gdf["geometry])).
            If `None` or empty list, returns array of zeros.
        window : :cls:`rasterio.windows.Window`
            A ``rasterio`` window defining the area of the raster. Can
            be used to speed up computation and decrease memory
            requirements if features are localized to a small portion of
            the raster array.

        Returns
        -------
        arr : ndarray
            Rasterized array of shapes.
        """
        logger.debug('Generating exclusion array of shape {}'
                     .format(self.arr_shape))
        log_mem(logger)

        shapes = shapes or []
        shapes = [(geom, 1) for geom in shapes if geom is not None]

        if self.inclusions:
            return self._rasterize_to_weights(shapes, window)

        return self._rasterize_to_mask(shapes, window)

    def _rasterize_to_weights(self, shapes, window):
        """Rasterize features to weights using a high-resolution array."""

        if not shapes:
            return ((1 - self._no_exclusions_array(window=window))
                    .astype(np.float32))

        hr_arr = self._no_exclusions_array(multiplier=self._scale_factor,
                                           window=window)
        transform = self._window_transform(window)
        transform *= transform.scale(1 / self._scale_factor)

        rio_features.rasterize(shapes=shapes, out=hr_arr, fill=0,
                               transform=transform)

        arr = self._aggregate_high_res(hr_arr, window)
        return 1 - (arr / self._scale_factor ** 2)

    def _rasterize_to_mask(self, shapes, window):
        """Rasterize features with to an exclusion mask."""

        arr = self._no_exclusions_array(window=window)
        if shapes:
            transform = self._window_transform(window)
            rio_features.rasterize(shapes=shapes, out=arr, fill=0,
                                   transform=transform)

        return arr

    def _aggregate_high_res(self, hr_arr, window):
        """Aggregate the high resolution exclusions array to output shape. """

        arr = self._no_exclusions_array(window=window).astype(np.float32)
        for i, j in product(range(self._scale_factor),
                            range(self._scale_factor)):
            arr += hr_arr[i::self._scale_factor, j::self._scale_factor]
        return arr

    def _window_transform(self, window):
        """Calculate the transform for a given window, if any. """
        if window is None:
            return deepcopy(self.transform)
        return windows.transform(window, self.transform)


class AbstractBaseSetbacks(AbstractBaseExclusionsMerger):
    """
    Create exclusions layers for setbacks
    """

    def __init__(self, excl_fpath, regulations, features, hsds=False,
                 weights_calculation_upscale_factor=None,
                 num_features_per_worker=1000):
        """
        Parameters
        ----------
        excl_fpath : str
            Path to .h5 file containing exclusion layers, will also be
            the location of any new setback layers
        regulations : `~reVX.setbacks.regulations.SetbackRegulations`
            A `SetbackRegulations` object used to extract setback
            distances.
        features : str
            Path to file containing features to compute exclusions from.
        hsds : bool, optional
            Boolean flag to use h5pyd to handle .h5 'files' hosted on
            AWS behind HSDS. By default `False`.
        weights_calculation_upscale_factor : int, optional
            If this value is an int > 1, the output will be a layer with
            **inclusion** weight values instead of exclusion booleans.
            For example, a cell that was previously excluded with a
            a boolean mask (value of 1) may instead be converted to an
            inclusion weight value of 0.75, meaning that 75% of the area
            corresponding to that point should be included (i.e. the
            exclusion feature only intersected a small portion - 25% -
            of the cell). This percentage inclusion value is calculated
            by upscaling the output array using this input value,
            rasterizing the exclusion features onto it, and counting the
            number of resulting sub-cells excluded by the feature. For
            example, setting the value to `3` would split each output
            cell into nine sub-cells - 3 divisions in each dimension.
            After the feature is rasterized on this high-resolution
            sub-grid, the area of the non-excluded sub-cells is totaled
            and divided by the area of the original cell to obtain the
            final inclusion percentage. Therefore, a larger upscale
            factor results in more accurate percentage values. However,
            this process is memory intensive and scales quadratically
            with the upscale factor. A good way to estimate your minimum
            memory requirement is to use the following formula:

            .. math:: memory (GB) = s_0 * s_1 * (sf^2 * 2 + 4) / 1073741824,

            where :math:`s_0` and :math:`s_1` are the dimensions (shape)
            of your exclusion layer and :math:`sf` is the scale factor
            (be sure to add several GB for any other overhead required
            by the rest of the process). If `None` (or a value <= 1),
            this process is skipped and the output is a boolean
            exclusion mask. By default `None`.
        """
        self._rasterizer = Rasterizer(excl_fpath,
                                      weights_calculation_upscale_factor, hsds)
        super().__init__(excl_fpath, regulations, features, hsds)
        self._features_meta = GPKGMeta(self._features)
        self.num_features_per_worker = num_features_per_worker

    def __repr__(self):
        msg = "{} for {}".format(self.__class__.__name__, self._excl_fpath)
        return msg

    def parse_features(self):
        """Method to parse features.

        Returns
        -------
        `geopandas.GeoDataFrame`
            Geometries of features to setback from in exclusion
            coordinate system.
        """
        logger.debug("Loading features from {}".format(self._features))
        return (gpd.read_file(self._features)
                .to_crs(crs=self.profile['crs']))

    @property
    def description(self):
        """str: Description to be added to excl H5."""
        return ('{} computed with a base setback distance of {} and a '
                'multiplier of {} for a total generic setback value of {} '
                '(local exclusions may differ).'
                .format(self.__class__,
                        self._regulations.base_setback_dist,
                        self._regulations.multiplier,
                        self._regulations.generic))

    @property
    def no_exclusions_array(self):
        """np.array: Array representing no exclusions. """
        return self._rasterizer.rasterize(shapes=None)

    @property
    def exclusion_merge_func(self):
        """callable: Function to merge overlapping exclusion layers. """
        return np.minimum if self._rasterizer.inclusions else np.maximum

    def pre_process_regulations(self):
        """Reduce regulations to state corresponding to features."""
        feats_crs = self._features_meta.crs
        xmin, ymin, xmax, ymax = self._features_meta.bbox
        regulations_df = self.regulations_table.to_crs(feats_crs)
        regulations_df = regulations_df.cx[xmin:xmax, ymin:ymax]
        regulations_df = regulations_df.to_crs(crs=self.profile['crs'])
        self._regulations.df = regulations_df.reset_index(drop=True)

        mask = self._regulation_table_mask()
        if not mask.any():
        # if  self._regulations.df.empty:
            msg = "Found no local regulations!"
            logger.warning(msg)
            warn(msg)

        self._regulations.df = (self.regulations_table[mask]
                                .reset_index(drop=True))
        logger.debug('Computing setbacks for regulations in {} counties'
                     .format(len(self.regulations_table)))

    def _local_exclusions_arguments(self, regulation_value, county):
        """Compile and return arguments to `compute_local_exclusions`. """
        logger.debug("Selecting county IDs using bounds {}"
                     .format(county.total_bounds))
        county = (county.buffer(regulation_value * 1.1)
                  .to_crs(self._features_meta.crs))
        ids = self._features_meta.feat_ids_for_bbox(county.total_bounds)
        logger.debug("Calculating setbacks for counties with IDs {}"
                     .format(ids))

        for start in range(0, len(ids), self.num_features_per_worker):
            end = start + self.num_features_per_worker
            yield (ids[start:end], self._features,
                   self._features_meta.primary_key_column,
                   self.profile['crs'], self._feature_filter, self._buffer,
                   self._rasterizer)

    @staticmethod
    def compute_local_exclusions(regulation_value, county, *args):
        """Compute local features setbacks.

        This method will compute the setbacks using a county-specific
        regulations file that specifies either a static setback or a
        multiplier value that will be used along with the base setback
        distance to compute the setback.

        Parameters
        ----------
        regulation_value : float | int
            Setback distance in meters.
        county : geopandas.GeoDataFrame
            Regulations for a single county.
        features_ids : iterable of ints
            List of tuple (or other iterable) of integer values
            corresponding to the ID of the features in the GeoPackage
            to load and compute exclusions for. Note that these ID
            values are the internal SQL table ID's stored with the
            features, NOT the index of the features when loaded using
            :func:`geopandas.read_file`.
        features_fp : path-like
            Path to the GeoPackage file containing the features to be
            loaded and used for the exclusion calculation.
        col : str
            Namer of the primary key column in the main SQL table of the
            GeoPackage. This should be the name of the column under
            which the `features_ids` can be found.
        crs : str
            String representation of teh Coordinate Reference System of
            the output exclusions array.
        feature_filter : callable
            A callable function that takes `features` and `county` as
            inputs and outputs a geopandas.GeoDataFrame with features
            clipped and/or localized to the input county.
        features_buffer : callable
            A callable function that takes `features` and
            `regulation_value` as inputs and outputs a list of shapes
            that represent buffered exclusions for the input county.
        rasterizer : Rasterizer
            Instance of `Rasterizer` class used to rasterize the
            buffered county features.

        Returns
        -------
        setbacks : ndarray
            Raster array of setbacks
        slices : 2-tuple of `slice`
            X and Y slice objects defining where in the original array
            the exclusion data should go.
        """
        (features_ids, features_fp, col, crs, features_filter,
         features_buffer, rasterizer) = args
        logger.debug('- Computing setbacks for county FIPS {}'
                     .format(county.iloc[0]['FIPS']))
        log_mem(logger)
        features = _load_features(features_ids, features_fp, col, crs)
        features = features_filter(features, county)
        features = features_buffer(features, regulation_value)
        return _rasterize_within_window(features, county, rasterizer)

    @staticmethod
    def _buffer(features, regulation_value):
        """Buffer features for county and return as list. """
        return list(features.buffer(regulation_value))

    def compute_generic_exclusions(self, max_workers=None):
        """Compute generic setbacks.

        This method will compute the setbacks using a generic setback
        of `base_setback_dist * multiplier`.

        Parameters
        ----------
        max_workers : int, optional
            Number of workers to use for exclusions computation, if 1
            run in serial, if > 1 run in parallel with that many
            workers, if `None` run in parallel on all available cores.
            By default `None`.

        Returns
        -------
        setbacks : ndarray
            Raster array of setbacks
        """
        generic_regulations_dne = (self._regulations.generic is None
                                   or np.isclose(self._regulations.generic, 0))
        if generic_regulations_dne:
            return self.no_exclusions_array

        max_workers = max_workers or os.cpu_count()
        ids = self._features_meta.feat_ids
        pk = self._features_meta.primary_key_column
        crs = self.profile['crs']
        exclusions = None
        if max_workers > 1:
            msg = ("Computing generic setbacks from {} features using {} "
                   "workers".format(len(ids), max_workers))
            logger.info(msg)

            loggers = [__name__, 'reVX', 'rex']
            spp_kwargs = {"max_workers": max_workers, "loggers": loggers}
            with SpawnProcessPool(**spp_kwargs) as exe:
                futures = []
                for start in range(0, len(ids), self.num_features_per_worker):
                    end = start + self.num_features_per_worker
                    future = exe.submit(_compute_exclusions, ids[start:end],
                                        self._features, pk, crs, self._buffer,
                                        self._regulations.generic,
                                        self._rasterizer)
                    futures.append(future)

                for future in as_completed(futures):
                    new_exclusions, slices = future.result()
                    futures.remove(future)
                    exclusions = self._combine_exclusions(exclusions,
                                                          new_exclusions,
                                                          slices=slices)

        else:
            logger.info("Computing generic setbacks from {} features in "
                        "serial.".format(len(ids)))
            for start in range(0, len(ids), self.num_features_per_worker):
                end = start + self.num_features_per_worker
                out = _compute_exclusions(ids[start:end], self._features, pk,
                                          crs, self._buffer,
                                          self._regulations.generic,
                                          self._rasterizer)
                new_exclusions, slices = out
                exclusions = self._combine_exclusions(exclusions,
                                                      new_exclusions,
                                                      slices=slices)

        return exclusions

    @classmethod
    def input_output_filenames(cls, out_dir, features_fpath, *__, **___):
        """Generate pairs of input/output file names.

        Parameters
        ----------
        out_dir : str
            Path to output file directory.
        features_fpath : str
            Path to features file. This path can contain
            any pattern that can be used in the glob function.
            For example, `/path/to/features/[A]*` would match
            with all the features in the directory
            `/path/to/features/` that start with "A". This input
            can also be a directory, but that directory must ONLY
            contain feature files. If your feature files are mixed
            with other files or directories, use something like
            `/path/to/features/*.geojson`.
        kwargs : dict
            Dictionary of extra keyword-argument pairs used to
            instantiate the `exclusion_class`.

        Yields
        ------
        tuple
            An input-output filename pair.
        """
        for fpath in cls.get_feature_paths(features_fpath):
            fn = os.path.basename(fpath)
            geotiff = ".".join(fn.split('.')[:-1] + ['tif'])
            yield fpath, os.path.join(out_dir, geotiff)

    @staticmethod
    def get_feature_paths(features_fpath):
        """Ensure features path exists and return as list.

        Parameters
        ----------
        features_fpath : str
            Path to features file. This path can contain
            any pattern that can be used in the glob function.
            For example, `/path/to/features/[A]*` would match
            with all the features in the directory
            `/path/to/features/` that start with "A". This input
            can also be a directory, but that directory must ONLY
            contain feature files. If your feature files are mixed
            with other files or directories, use something like
            `/path/to/features/*.geojson`.

        Returns
        -------
        features_fpath : list
            Features path as a list of strings.

        Notes
        -----
        This method is required for `run` class methods for
        feature setbacks that are spread out over multiple
        files.
        """
        glob_path = pathlib.Path(features_fpath)
        if glob_path.is_dir():
            glob_path = glob_path / '*'

        paths = [str(f) for f in glob_path.parent.glob(glob_path.name)]
        if not paths:
            msg = 'No files found matching the input {!r}!'
            msg = msg.format(features_fpath)
            logger.error(msg)
            raise FileNotFoundError(msg)

        return paths

    @staticmethod
    def _feature_filter(features, county):
        """Filter the features given a county."""
        return features_with_centroid_in_county(features, county)

    @abstractmethod
    def _regulation_table_mask(self):
        """Return the regulation table mask for setback feature. """
        raise NotImplementedError


def _parse_excl_properties(excl_fpath, hsds=False):
    """Parse shape, chunk size, and profile from exclusions file.

    Parameters
    ----------
    excl_fpath : str
        Path to .h5 file containing exclusion layers, will also be
        the location of any new setback layers
    hsds : bool, optional
        Boolean flag to use h5pyd to handle .h5 'files' hosted on
        AWS behind HSDS. By default `False`.

    Returns
    -------
    shape : tuple
        Shape of exclusions datasets
    profile : str
        GeoTiff profile for exclusions datasets
    """
    with ExclusionLayers(excl_fpath, hsds=hsds) as exc:
        dset_shape = exc.shape
        profile = exc.profile

    if len(dset_shape) < 3:
        dset_shape = (1, ) + dset_shape

    logger.debug('Exclusions properties:\n'
                 'shape : {}\n'
                 'profile : {}\n'
                 .format(dset_shape, profile))

    return dset_shape, profile


def _load_features(features_ids, features_fp, col, crs):
    """Load the `features_ids` from the `features_fp`. """
    ids = ",".join(map(str, features_ids))
    logger.debug("  Loading {} features from {}".format(len(features_ids),
                                                        features_fp))
    features = gpd.read_file(features_fp,
                             where="{} in ({})".format(col, ids))
    features = features.to_crs(crs=crs)
    logger.debug("  Loaded {} features".format(len(features)))
    return features


def _compute_exclusions(features_ids, features_fp, col, crs, features_buffer,
                        setback, rasterizer):
    """Compute exclusions by loading features, buffering, and rasterizing. """
    features = _load_features(features_ids, features_fp, col, crs)
    setbacks = features_buffer(features, setback)
    return _rasterize_within_window(setbacks, features.buffer(setback * 2),
                                    rasterizer)


def _rasterize_within_window(features, geoseries, rasterizer):
    """Rasterize the features using the geoseries bounding box as a window. """
    window = _cropped_window(geoseries, rasterizer.transform,
                             rasterizer.arr_shape[1:])
    exclusions = rasterizer.rasterize(features, window=window)
    return exclusions, window.toslices()


def _cropped_window(geoseries, raster_transform, shape):
    """Calculate the raster array window corresponding to the bounding box."""
    buffer_len = max(abs(raster_transform.a), abs(raster_transform.e))
    left, bottom, right, top = geoseries.buffer(buffer_len).total_bounds

    rows, cols = transform.rowcol(raster_transform,
                                  [left, right, right, left],
                                  [top, top, bottom, bottom],
                                  op=float)

    row_start = max(floor(min(rows)), 0)
    col_start = max(floor(min(cols)), 0)
    row_stop = min(ceil(max(rows)), shape[0])
    col_stop = min(ceil(max(cols)), shape[1])
    return windows.Window(col_off=col_start, row_off=row_start,
                          width=max(col_stop - col_start, 0.0),
                          height=max(row_stop - row_start, 0.0))
