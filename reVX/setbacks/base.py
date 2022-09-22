# -*- coding: utf-8 -*-
"""
Compute setbacks exclusions
"""
import os
from abc import abstractmethod
from warnings import warn
from itertools import product
import logging
import pathlib
import numpy as np
import geopandas as gpd
from rasterio import features

from rex.utilities import log_mem
from reV.handlers.exclusions import ExclusionLayers
from reVX.utilities.exclusions import AbstractBaseExclusionsMerger

logger = logging.getLogger(__name__)


def features_with_centroid_in_county(features, cnty):
    """Find features with centroids within the given county.

    Parameters
    ----------
    features : geopandas.GeoDataFrame
        Features to setback from.
    cnty : geopandas.GeoDataFrame
        Regulations for a single county.

    Returns
    -------
    features : geopandas.GeoDataFrame
        Features that have centroid in county.
    """

    mask = features.centroid.within(cnty['geometry'].values[0])
    return features.loc[mask]


def features_clipped_to_county(features, cnty):
    """Clip features to the given county geometry.

    Parameters
    ----------
    features : geopandas.GeoDataFrame
        Features to setback from.
    cnty : geopandas.GeoDataFrame
        Regulations for a single county.

    Returns
    -------
    features : geopandas.GeoDataFrame
        Features clipped to county geometry.
    """
    tmp = gpd.clip(features, cnty)
    return tmp[~tmp.is_empty]


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
    def arr_shape(self):
        """Rasterize array shape.

        Returns
        -------
        tuple
        """
        return self._shape

    def _no_exclusions_array(self, multiplier=1):
        """Get an array of the correct shape representing no exclusions.

        The array contains all zeros, and a new one is created
        for every function call.

        Parameters
        ----------
        multiplier : int, optional
            Integer multiplier value used to scale up the dimensions of
            the array exclusions array (e.g. multiplier of 3 turns an
            array of shape (10, 20) into an array of shape (30, 60)).

        Returns
        -------
        np.array
            Array of zeros representing no exclusions.
        """
        high_res_shape = tuple(x * multiplier for x in self.arr_shape[1:])
        return np.zeros(high_res_shape, dtype='uint8')

    def rasterize(self, shapes):
        """Convert geometries into exclusions array.

        Parameters
        ----------
        shapes : list, optional
            List of shapes to rasterize. If `None` or empty list,
            returns array of zeros.

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

        if self._scale_factor > 1:
            return self._rasterize_to_weights(shapes)

        return self._rasterize_to_mask(shapes)

    def _rasterize_to_weights(self, shapes):
        """Rasterize features to weights using a high-resolution array."""

        if not shapes:
            return 1 - self._no_exclusions_array()

        hr_arr = self._no_exclusions_array(multiplier=self._scale_factor)
        new_transform = list(self.profile['transform'])[:6]
        new_transform[0] = new_transform[0] / self._scale_factor
        new_transform[4] = new_transform[4] / self._scale_factor

        features.rasterize(shapes=shapes,
                           out=hr_arr,
                           out_shape=hr_arr.shape[1:],
                           fill=0,
                           transform=new_transform)

        arr = self._aggregate_high_res(hr_arr)
        return 1 - (arr / self._scale_factor ** 2)

    def _rasterize_to_mask(self, shapes):
        """Rasterize features with to an exclusion mask."""

        arr = self._no_exclusions_array()
        if shapes:
            features.rasterize(shapes=shapes,
                               out=arr,
                               out_shape=arr.shape[1:],
                               fill=0,
                               transform=self.profile['transform'])

        return arr

    def _aggregate_high_res(self, hr_arr):
        """Aggregate the high resolution exclusions array to output shape. """

        arr = self._no_exclusions_array().astype(np.float32)
        for i, j in product(range(self._scale_factor),
                            range(self._scale_factor)):
            arr += hr_arr[i::self._scale_factor, j::self._scale_factor]
        return arr


class AbstractBaseSetbacks(AbstractBaseExclusionsMerger):
    """
    Create exclusions layers for setbacks
    """

    def __init__(self, excl_fpath, regulations, hsds=False,
                 weights_calculation_upscale_factor=None):
        """
        Parameters
        ----------
        excl_fpath : str
            Path to .h5 file containing exclusion layers, will also be
            the location of any new setback layers
        regulations : `~reVX.setbacks.regulations.Regulations`
            A `Regulations` object used to extract setback distances.
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
        super().__init__(excl_fpath, regulations, hsds)

    def __repr__(self):
        msg = "{} for {}".format(self.__class__.__name__, self._excl_fpath)
        return msg

    @property
    def profile(self):
        """dict: Geotiff profile. """
        return self._rasterizer.profile

    def parse_features(self):
        """Method to parse features.

        Returns
        -------
        `geopandas.GeoDataFrame`
            Geometries of features to setback from in exclusion
            coordinate system.
        """
        return (gpd.read_file(self._features_fpath)
                .to_crs(crs=self.profile['crs']))

    def pre_process_regulations(self):
        """Reduce regulations to state corresponding to features_fpath.

        """
        mask = self._regulation_table_mask()
        if not mask.any():
            msg = "Found no local regulations!"
            logger.warning(msg)
            warn(msg)

        self._regulations.regulations = (self.regulations_table[mask]
                                         .reset_index(drop=True))
        logger.debug('Computing setbacks for regulations in {} counties'
                     .format(len(self.regulations_table)))

    def compute_local_exclusions(self, regulation_value, cnty):
        """Compute local features setbacks.

        This method will compute the setbacks using a county-specific
        regulations file that specifies either a static setback or a
        multiplier value that will be used along with the base setback
        distance to compute the setback.

        Parameters
        ----------
        regulation_value : float | int
            Setback distance in meters.
        cnty : geopandas.GeoDataFrame
            Regulations for a single county.

        Returns
        -------
        setbacks : ndarray
            Raster array of setbacks
        """
        logger.debug('- Computing setbacks for county FIPS {}'
                     .format(cnty.iloc[0]['FIPS']))
        features = self.parse_features()
        idx = features.sindex.intersection(cnty.total_bounds)
        features = features.iloc[list(idx)].copy()
        log_mem(logger)
        features = self._feature_filter(features, cnty)
        features = list(features.buffer(regulation_value))
        return self._rasterizer.rasterize(features)

    def compute_generic_exclusions(self, **__):
        """Compute generic setbacks.

        This method will compute the setbacks using a generic setback
        of `base_setback_dist * multiplier`.

        Returns
        -------
        setbacks : ndarray
            Raster array of setbacks
        """
        logger.info('Computing generic setbacks')
        if np.isclose(self._regulations.generic, 0):
            return self._rasterizer.rasterize(shapes=None)

        setback_features = self.parse_features()
        setbacks = list(setback_features.buffer(self._regulations.generic))

        return self._rasterizer.rasterize(setbacks)

    def input_output_filenames(self, out_dir, features_fpath):
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

        Yields
        ------
        tuple
            An input-output filename pair.
        """
        for fpath in self.get_feature_paths(features_fpath):
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
        This method is required for `run` classmethods for
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
    def _feature_filter(features, cnty):
        """Filter the features given a county."""
        return features_with_centroid_in_county(features, cnty)

    @abstractmethod
    def _regulation_table_mask(self):
        """Return the regulation table mask for setback feature. """
        raise NotImplementedError
