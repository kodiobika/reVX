"""
Various utility functions to prep data for offshore least-cost paths analysis.

Mike Bannister 5/2022
"""
import os
import json
import logging
from functools import reduce

import h5py
import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio as rio
from rasterio import features
from rasterio.warp import reproject, Resampling
from shapely.geometry import Point, LineString

import rex

logger = logging.getLogger(__name__)


def _sum(a, b):
    return a + b


def convert_pois_to_lines(poi_csv_f: str, template_f: str, out_f: str):
    """
    Convert POIs in CSV to lines and save in a geopackage as substations. Also
    create a fake transmission line to connect to the substations.

    Parameters
    ----------
    poi_csv_f
        Path to CSV file with POIs in it
    template_f
        Path to template raster with CRS to use for geopackage
    out_f
        Path and file name for geopackage
    """
    logger.info('Converting POIs in {} to lines in {}'
                .format(poi_csv_f, out_f))
    with rio.open(template_f) as ras:
        crs = ras.crs

    df = pd.read_csv(poi_csv_f)[['POI Name', 'State', 'Voltage (kV)', 'Lat',
                                 'Long']]

    pts = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.Long, df.Lat))
    pts = pts.set_crs('EPSG:4326')
    pts = pts.to_crs(crs)

    # Convert points to short lines
    new_geom = []
    for pt in pts.geometry:
        end = Point(pt.x + 50, pt.y + 50)
        line = LineString([pt, end])
        new_geom.append(line)
    lines = pts.set_geometry(new_geom, crs=crs)

    # Append some fake values to make the LCP code happy
    lines['ac_cap'] = 9999999
    lines['category'] = 'Substation'
    lines['voltage'] = 500  # kV
    lines['trans_gids'] = '[9999]'

    # add a fake trans line for the subs to connect to to make LCP code happy
    trans_line = pd.DataFrame(
        {
            'POI Name': 'fake',
            'ac_cap': 9999999,
            'category': 'TransLine',
            'voltage': 500,  # kV
            'trans_gids': None
        },
        index=[9999]
    )

    trans_line = gpd.GeoDataFrame(trans_line)
    geo = LineString([Point(0, 0), Point(100000, 100000)])
    trans_line = trans_line.set_geometry([geo], crs=crs)

    pois: gpd.GeoDataFrame = pd.concat([lines, trans_line])
    pois['gid'] = pois.index

    pois.to_file(out_f, driver="GPKG")
    logger.info('Complete')


class CombineRasters:
    """
    Combine layers to create composite friction and barrier rasters. Merge
    with existing land cost and barriers and save to h5.
    """
    OFFSHORE_FRICTION_FNAME = 'offshore_friction.tif'
    OFFSHORE_BARRIERS_FNAME = 'offshore_barriers.tif'
    COMBO_LAYER_FNAME = 'combo_{}.tif'
    LAND_MASK_FNAME = 'land_mask.tif'

    def __init__(self, template_f, layer_dir='', slope_barrier_cutoff=15,
                 low_slope_cutoff=10, high_slope_friction=10,
                 medium_slope_friction=5, low_slope_friction=1):
        """
        Parameters
        ----------
        template_f : str
            Path to template raster with CRS to use for geopackage
        layer_dir : str, optional
            Directory to prepend to barrier and friction layer filenames
        slope_barrier_cutoff : float
            Slopes >= this value are set to high_slope_friction and used as
            barriers.
        low_slope_cutoff : float
            Slope < this value are assigned low_slope_friciton.
        high_slope_friction : int
            Used for >= slope_barrier_cutoff
        medium_slope_friction : int
            Used for < slope_barrier_cutoff and > low_slope_cutoff
        low_slope_friction : int
            Used for < low_slope_cutoff
        """
        self.layer_dir = layer_dir

        self._slope_barrier_cutoff = slope_barrier_cutoff
        self._low_slope_cutoff = low_slope_cutoff
        self._high_slope_friction = high_slope_friction
        self._medium_slope_friction = medium_slope_friction
        self._low_slope_friction = low_slope_friction

        self._os_profile = self._extract_profile(template_f)
        self._os_profile['dtype'] = ('MUST SET in {}.profile()!'
                                     .format(self.__class__.__name__))
        self._os_shape = (self.profile()['height'],
                          self.profile()['width'])

        self._os_barriers = None  # (uint8) offshore barrier raster
        self._os_friction = None  # (float32) offshore friction raster
        self._land_mask = None  # (bool) land mask raster, true indicates land

    def create_land_mask(self, mask_shp_f, save_tiff=False, filename=None,
                         buffer_dist=None):
        """
        Create the land mask layer from a vector file. Optionally, buffer all
        features by a distance before rasterizing, e.g., to create a near-shore
        friction layer.

        Parameters
        ----------
        mask_shp_f : str
            Full path to mask gpgk or shp file
        save_tiff : bool
            Save mask as tiff if true
        filename : str, optional
            Name of file to save rasterized mask to
        buffer_dist : int, optional
            Distance to buffer features in mask_shp_f by. Same units as the
            template raster.
        """
        ldf = gpd.read_file(mask_shp_f)
        if buffer_dist is not None:
            ldf.geometry = ldf.geometry.buffer(buffer_dist)

        logger.info('Rasterizing {}'.format(mask_shp_f))
        l_geom = list(ldf.geometry)
        l_rast = features.rasterize(l_geom, out_shape=self._os_shape, fill=0,
                                    out=None,
                                    transform=self.profile()['transform'],
                                    all_touched=False, default_value=1,
                                    dtype=None)
        if save_tiff:
            if filename is None:
                filename = self.LAND_MASK_FNAME
            logger.info('Saving land mask to {}'.format(filename))
            self._save_tiff(l_rast, filename)

        self._land_mask = l_rast == 1
        logger.info('Rasterizing complete')

    def load_land_mask(self, mask_f=None):
        """
        Load the land mask layer from a tiff. This does not need to be called
        if self.create_land_mask() was run previously.

        Parameters
        ----------
        mask_f : str
            Full path to mask tiff
        """
        if mask_f is None:
            mask_f = self.LAND_MASK_FNAME
        with rio.open(mask_f) as ras:
            l_rast = ras.read(1)

        assert l_rast.max() == 1
        assert l_rast.shape == self._os_shape

        self._land_mask = l_rast == 1
        logger.info('Successfully loaded land mask from {}'.format(mask_f))

    # flake8: noqa: C901
    def build_off_shore_friction(self, friction_files, slope_file=None,
                                 bathy_file=None, bathy_depth_cutoff=None,
                                 bathy_friction=None,
                                 minimum_friction_files=None, save_tiff=None):
        """
        Combine offshore friction layers.

        friction_files : list of tuples
            Friction layers to combine and raster value to friction dict.
            Tuples are in the format:

                ({ras_val1: fric_val1, ras_val2: fric_val2, ...}, 'fname.tif')

            where ras_valX is the value in the raster, and fric_valX is the
            desired friction values. Any values in the raster that are not
            specified in the dict are assumed to have no friction. 'fname.tif'
            is the file name of the raster.
        slope_file : str, optional
            Path to slope friction tiff
        bathy_file : str, optional
            Path to bathymetry tiff. Values are assumed to decrease with depth.
        bathy_depth_cutoff : float, optional
            Depth below which a friction is applied. This must in the same
            units as the bathy file.
        bathy_friction : int, optional
            Friction value to apply to areas with a depth great than
            bath_depth_cutoff.
        minimum_friction_files : list of tuples
            Same format as friction_files. Specified layers will be used to
            ensure a minimum friction is uesd. This is performed after all
            other friction layers have been combined.
        save_tiff : bool, optional
            Save composite friction to tiff if true
        """
        logger.info('Processing friction layers')
        fr_layers = {}

        # Add bathymetry to friction dict
        if bathy_file is not None:
            logger.info('--- calculating bathymetric friction')
            if bathy_depth_cutoff is None or bathy_friction is None:
                raise AttributeError('bathy_depth_cutoff and bathy_friction '
                                     'must be set if bath_file is set')

            logger.debug('--- --- bathy_depth_cutoff is %s',
                         bathy_depth_cutoff)
            logger.debug('--- --- bathy_friction is %s', bathy_friction)

            if not os.path.exists(bathy_file):
                bathy_file = os.path.join(self.layer_dir, bathy_file)
            if not os.path.exists(bathy_file):
                raise FileNotFoundError(f'Unable to find {bathy_file}')

            logger.debug('--- --- opening bathy data')
            d = rio.open(bathy_file).read(1)
            assert d.shape == self._os_shape
            logger.debug('--- --- assigning bathy friction')
            d2 = np.where(d >= bathy_depth_cutoff, 0, bathy_friction)

            fr_layers[bathy_file] = d2.astype('uint16')

        # Add slope to friction dict
        if slope_file is not None:
            logger.info('--- calculating slope friction')

            if not os.path.exists(slope_file):
                slope_file = os.path.join(self.layer_dir, slope_file)
            if not os.path.exists(slope_file):
                raise FileNotFoundError('Unable to find {}'.format(slope_file))

            d = rio.open(slope_file).read(1)
            d[d < 0] = 0
            assert d.shape == self._os_shape and d.min() == 0
            # Slope >= slope_barrier_cutoff is also included in barriers
            d2 = np.where(d >= self._slope_barrier_cutoff,
                          self._high_slope_friction, d)
            d2 = np.where(d < self._slope_barrier_cutoff,
                          self._medium_slope_friction, d2)
            d2 = np.where(d < self._low_slope_cutoff, self._low_slope_friction,
                          d2)
            fr_layers[slope_file] = d2.astype('uint16')

        # Add all other friction files to friction dict
        for fr_dict, f in friction_files:
            d = None
            for k, val in fr_dict.items():
                logger.info('--- setting raster value {} to friction '
                            '{} for {}'.format(k, val, f))
                tmp_d = self._load_layer(f, k) * val
                d = tmp_d if d is None else d + tmp_d

            assert d.shape == self._os_shape and d.min() == 0
            fr_layers[f] = d.astype('uint16')

        logger.info('--- combining all offshore friction layers')
        self._os_friction = reduce(_sum, fr_layers.values()).astype('uint16')

        # Set minimum friction if used
        if minimum_friction_files is not None:
            for fr_dict, f in minimum_friction_files:
                d = None
                for k, val in fr_dict.items():
                    logger.info('--- setting raster value %s to minimum '
                                'friction %s for %s', k, val, f)
                    tmp_d = self._load_layer(f, k) * val
                    d = tmp_d if d is None else np.maximum(d, tmp_d)

                assert d.shape == self._os_shape and d.min() >= 0

                self._os_friction = np.maximum(d.astype('uint16'),
                                            self._os_friction)

        if save_tiff:
            logger.info('Saving combined friction to tiff')
            self._save_tiff(self._os_friction, self.OFFSHORE_FRICTION_FNAME)

        logger.info('Done processing friction layers')

    def build_off_shore_barriers(self, barrier_files, fi_files,
                                 slope_file=None, save_tiff=False):
        """
        Combine offshore barrier layers

        Parameters
        ----------
        barrier_files : list of tuples (int|list, str)
            Barrier layers to combine. Tuples are in one of two formats:
                (X, 'fname.tif') or
                ([X1, X2, ...], 'fname.tif')
            Where 'fname.tif' is the raster file, and X is the raster value
            to use as the barrier. Alternatively, a list of multiple values can
            be used as barriers. Any other values in the raster are assumed
            to be open to tranmission.
        fi_files : list of tuples (int, str)
            Force include layers. These will override the barrier layers.
            Tuple format is the same as for barrier_files, however the specifed
            raster values are force included.
        slope_file : str, optional
            Path to slope tiff
        save_tiff : bool, options
            Save composite layer to geotiff if true
        """
        logger.info('Loading barrier layers')
        barrier_layers = {}
        for val, f in barrier_files:
            logger.info('--- {}'.format(f))
            d = self._load_layer(f, val)
            assert d.shape == self._os_shape and d.min() == 0 and d.max() == 1
            barrier_layers[f] = d

        if slope_file is not None:
            logger.info('--- calculating slope barrier')
            if not os.path.exists(slope_file):
                slope_file = os.path.join(self.layer_dir, slope_file)
            if not os.path.exists(slope_file):
                raise FileNotFoundError('Unable to find {}'.format(slope_file))

            d = rio.open(slope_file).read(1)
            assert d.shape == self._os_shape
            d2 = np.where(d < self._slope_barrier_cutoff, 0, d)
            d2 = np.where(d >= self._slope_barrier_cutoff, 1, d2)

            barrier_layers[slope_file] = d2.astype('uint8')

        # Add all the exclusion layers together and normalize
        logger.info('Building composite offshore barrier layers')
        comp_bar = reduce(_sum, barrier_layers.values())
        comp_bar[comp_bar >= 1] = 1

        if len(fi_files) > 0:
            logger.info('Loading forced inclusion layers')
            fi_layers = {}
            for val, f in fi_files:
                logger.info('--- {}'.format(f))
                d = self._load_layer(f, val)
                assert d.shape == self._os_shape and d.min() == 0 and \
                    d.max() == 1
                fi_layers[f] = d

            logger.info('Building composite forced inclusion layers')
            comp_fi = reduce(_sum, fi_layers.values())
            comp_fi[comp_fi >= 1] = 1

            # Subtract fi from barriers
            comp_bar = comp_bar.astype('int8') - comp_fi.astype('int8')
            comp_bar[comp_bar < 0] = 0

        assert comp_bar.max() == 1
        assert comp_bar.min() == 0

        if save_tiff:
            logger.info('Saving barriers to {}'
                        .format(self.OFFSHORE_BARRIERS_FNAME))
            self._save_tiff(comp_bar, self.OFFSHORE_BARRIERS_FNAME)

        self._os_barriers = comp_bar
        logger.info('Done building barrier layers')

    def merge_os_and_land_friction(self, land_h5, land_cost_layer,
                                   offshore_h5, os_friction_layer=None,
                                   os_friction_f=None, land_cost_mult=1,
                                   save_tiff=False):
        """
        Combine offshore friction and land cost layers and save to h5. For
        land it's called cost for legacy reasons, and for offshore it's called
        friction, but it's really the same thing.

        Parameters
        ----------
        land_h5 : str
            Path to h5 file w/ land barrier
        land_cost_layer : str
            Name of land barrier layer in h5 to use
        offshore_h5 : str
            Path to h5 file to save combined friction in
        os_friction_layer : str | None, optional
            Name for friction layer in offshore h5. Use land_cost_layer
            if None.
        os_friction_f : str | None, optional
            Path to cached offshore friction raster. If None, will try to pull
            data from self._os_friction
        land_cost_mult : float, optional
            Multiplier for land costs
        save_tiff : bool, optional
            Save composite barrier layer to geotiff if true
        """
        # Try to load friction from self first, then look for tiff
        if self._os_friction is not None:
            os_friction = self._os_friction
        else:
            if os_friction_f is None:
                os_friction_f = self.OFFSHORE_FRICTION_FNAME
            if not os.path.exists(os_friction_f):
                msg = ('Offshore friction has not been calculated and cached'
                       ' friction was not found at {}. Please run {}.'
                       'build_off_shore_friction() first or pass a valid '
                       'filename to os_friction_f'
                       .format(os_friction_f, self.__class__.__name__))
                raise AttributeError(msg)

            logger.info('Loading offshore friction from {}'
                        .format(os_friction_f))
            with rio.open(os_friction_f) as ras:
                os_friction = ras.read(1)

        if os_friction_layer is None:
            os_friction_layer = land_cost_layer
        self._merge_os_and_land_layers(os_friction, land_h5, land_cost_layer,
                                       offshore_h5, os_friction_layer,
                                       layer_name='friction',
                                       land_mult=land_cost_mult,
                                       save_tiff=save_tiff,
                                       dtype='float32')

    def merge_os_and_land_barriers(self, land_h5, land_barrier_layer,
                                   offshore_h5, os_barrier_layer=None,
                                   os_barriers_f=None, save_tiff=False):
        """
        Combine offshore and land barrier layers and save to h5

        Parameters
        ----------
        land_h5 : str
            Path to h5 file w/ land barrier
        land_barrier_layer : str
            Name of land barrier layer in h5 to use
        offshore_h5 : str
            Path to h5 file to save combined barriers in
        os_barrier_layer : str | None
            Name for barrier layer in offshore h5. Use land_barrier_layer
            if None.
        os_barriers_f : str | None, optional
            Path to offshore barrier raster. If None, will try to pull data
            from self._os_barriers
        save_tiff : bool, options
            Save composite barrier layer to geotiff if true
        """
        # Try to load barriers from self first, then look for tiff
        if self._os_barriers is not None:
            os_barriers = self._os_barriers
        else:
            if os_barriers_f is None:
                os_barriers_f = self.OFFSHORE_BARRIERS_FNAME
            if not os.path.exists(os_barriers_f):
                msg = ('Offshore barriers have not been calculated and cached'
                       ' barriers were not found at {}. Please run {}.'
                       'build_off_shore_barriers() first or pass a valid '
                       'filename to os_barriers_f'
                       .format(os_barriers_f, self.__class__.__name__))
                raise AttributeError(msg)

            logger.info('Loading offshore barriers from {}'
                        .format(os_barriers_f))
            with rio.open(os_barriers_f) as ras:
                os_barriers = ras.read(1)

        if os_barrier_layer is None:
            os_barrier_layer = land_barrier_layer

        self._merge_os_and_land_layers(os_barriers, land_h5,
                                       land_barrier_layer, offshore_h5,
                                       os_barrier_layer, layer_name='barriers',
                                       save_tiff=save_tiff)

    def _merge_os_and_land_layers(self, os_data, land_h5, land_layer,
                                  offshore_h5, os_layer,
                                  layer_name='data',
                                  land_mult=1, dtype='uint8', save_tiff=False,
                                  init_dest=-1):
        """
        Combine offshore and land layers and save to h5

        Parameters
        ----------
        os_data : np.ndarray
            Offshore data to merge with land data and save to h5
        land_h5 : str
            Path to h5 file w/ land layer
        land_layer : str
            Name of land layer in h5 to use
        offshore_h5 : str
            Path to h5 file to save combined layer in
        os_layer : str
            Name for layer in offshore h5.
        layer_name : str, optional
            Layer name for printing status, saving to tiff, and storing
            combined data on self
        land_mult : float, optional
            Multiplier for values in land layer
        dtype : str
            Data type to use for combined raster
        save_tiff : bool, options
            Save composite barrier layer to geotiff if true
        init_dest : int | float
            Initial value to seed combined raster
        """
        # Load land layer
        logger.info('Loading land {} "{}" from {}'
                    .format(layer_name, land_layer, land_h5))
        with rex.Resource(land_h5) as res:
            profile_json = res.attrs[land_layer]['profile']
            old_land_profile = json.loads(profile_json)
            old_land_data = res[land_layer][0]

        # Reproject land barriers to new offshore projection
        logger.info('Reprojecting land {}'.format(layer_name))
        land_data = np.ones(self._os_shape, dtype=dtype)
        reproject(old_land_data,
                  destination=land_data,
                  src_transform=old_land_profile['transform'],
                  src_crs=old_land_profile['crs'],
                  dst_transform=self.profile()['transform'],
                  dst_crs=self.profile()['crs'],
                  dst_resolution=self._os_shape, num_threads=5,
                  resampling=Resampling.nearest,
                  INIT_DEST=init_dest)

        assert os_data.shape == land_data.shape
        setattr(self, '_land_{}'.format(layer_name), land_data)

        # Combine the land and offshore data
        logger.info('Combining land and offshore {}'.format(layer_name))
        combo_data = land_data * land_mult
        # pylint: disable=invalid-unary-operand-type
        combo_data[~self.land_mask] = os_data[~self.land_mask]
        combo_data = combo_data.astype(dtype)

        if save_tiff:
            fname = self.COMBO_LAYER_FNAME.format(layer_name)
            logger.info('Saving offshore %s combined with %s combined %s to '
                        '%s', layer_name, land_layer, layer_name, fname)
            self._save_tiff(combo_data, fname)

        setattr(self, '_combo_{}'.format(layer_name), combo_data)

        logger.info('Writing offshore %s combined with land "%s" to "%s" in '
                    '%s', layer_name, land_layer, os_layer, offshore_h5)
        combo_data = combo_data[np.newaxis, ...]
        with h5py.File(offshore_h5, 'a') as f:
            if os_layer in f.keys():
                dset = f[os_layer]
                dset[...] = combo_data
            else:
                f.create_dataset(os_layer, data=combo_data)

    def create_offshore_h5(self, ex_h5, offshore_h5, overwrite=False):
        """
        Create a new h5 file to save offshore data in.

        Parameters
        ----------
        ex_h5 : str
            Path to existing h5 file w/ offshore shape
        offshore_h5 : str
            Path for new h5 file to create
        overwrite : bool, optional
            Overwrite existing h5 file if True

        """
        if os.path.exists(offshore_h5) and not overwrite:
            raise AttributeError('File {} exits'.format(offshore_h5))

        with rex.Resource(ex_h5) as res:
            lats = res['latitude']
            lngs = res['longitude']
            global_attrs = res.global_attrs

        assert lats.shape == self._os_shape
        regions = np.ones(self._os_shape, dtype='uint8')

        with h5py.File(offshore_h5, 'w') as f:
            f.create_dataset('longitude', data=lngs)
            f.create_dataset('latitude', data=lats)
            f.create_dataset('ISO_regions', data=regions[np.newaxis, ...],
                             dtype='uint8')
            for k, v in global_attrs.items():
                f.attrs[k] = v

    @property
    def land_mask(self):
        """np.ndarray: Land mask layer."""
        if self._land_mask is None:
            cls_name = self.__class__.__name__
            msg = ('Must run {0}.create_land_mask() or {0}.'
                   'load_land_mask() first'.format(cls_name))
            raise RuntimeError(msg)

        return self._land_mask

    def profile(self, dtype=None):
        """Copy CRS Profile.

        Parameters
        ----------
        dtype : str, optional
            Optional dtype fill. By default, `None`.

        Returns
        -------
        dict
            CRS Profile.
        """
        prof = self._os_profile.copy()
        if dtype:
            prof['dtype'] = dtype
        return prof

    @staticmethod
    def _extract_profile(template_f):
        """Extract profile from file. """
        with rio.open(template_f) as ras:
            profile = {
                'crs': ras.crs,
                'transform': ras.transform,
                'height': ras.height,
                'width': ras.width,
                # 'dtype': ras.dtype,
                'count': 1,
                'compress': 'lzw'
            }
        return profile

    def _load_layer(self, f, val, verbose=False):
        """
        Load a layer from a tiff and set appropriate cells to 1

        Parameters
        ----------
        f : str
            File to load
        val : int | list of int
            Value(s) in file to set to a value of 1. All other values are set
            to zero.

        Returns
        -------
        d : np.ndarray
            Raster layer
        """
        if not os.path.exists(f):
            f_old = f
            f = os.path.join(self.layer_dir, f)
        if not os.path.exists(f):
            raise FileNotFoundError('Unable to find file {} or {}'
                                    .format(f_old, f))

        name = f.split('/')[-1]
        if verbose:
            logger.info('Processing val {} for {}'.format(val, name))
        d = rio.open(f).read(1)
        if isinstance(val, int):
            d[d != val] = 0
            d[d == val] = 1
        elif isinstance(val, list):
            d[~np.in1d(d, val).reshape(d.shape)] = 0
            d[np.in1d(d, val).reshape(d.shape)] = 1
        else:
            raise AttributeError('Unknown type for val: {} - {}'
                                 .format(val, type(val)))
        if verbose:
            logger.info(d.shape, d.max(), d.min())
        return d

    def _save_tiff(self, data, f_name):
        """
        Save data to a geotiff

        Parameters
        ----------
            data : np.array
                Data to save
            f_name : str
                File name to save
        """
        dtype = data.dtype
        with rio.open(f_name, 'w', **self.profile(dtype=dtype)) as outf:
            outf.write(data, indexes=1)
