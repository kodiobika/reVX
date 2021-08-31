# -*- coding: utf-8 -*-
"""
Module to compute least cost xmission paths, distances, and costs one or
more SC points
"""
from concurrent.futures import as_completed
import geopandas as gpd
import logging
import numpy as np
import os
import pandas as pd
from pyproj.crs import CRS
import rasterio
import time

from reV.handlers.exclusions import ExclusionLayers
from rex.utilities.execution import SpawnProcessPool

from reVX.least_cost_xmission.trans_cap_costs import TieLineCosts
from reVX.utilities.exclusions_converter import ExclusionsConverter

logger = logging.getLogger(__name__)


class LeastCostPaths:
    """
    Compute least cost paths between desired locations
    """
    REQUIRED_LAYRES = ['transmission_barrier']

    def __init__(self, cost_fpath, features_fpath, xmission_config=None):
        """
        Parameters
        ----------
        cost_fpath : str
            Path to h5 file with cost rasters and other required layers
        features_fpath : str
            Path to geopackage with transmission features
        resolution : int, optional
            SC point resolution, by default 128
        xmission_config : str | dict | XmissionConfig, optional
            Path to Xmission config .json, dictionary of Xmission config
            .jsons, or preloaded XmissionConfig objects, by default None
        """
        self._check_layers(cost_fpath)
        self._config = TieLineCosts._parse_config(
            xmission_config=xmission_config)

        self._features, self._shape = self._map_to_costs(
            cost_fpath, gpd.read_file(features_fpath))
        self._features = self._features.drop(columns='geometry')
        self._cost_fpath = cost_fpath

        logger.debug('Data loaded')

    def __repr__(self):
        msg = ("{} to be computed for "
               .format(self.__class__.__name__))

        return msg

    @property
    def features(self):
        """
        Table of features to compute paths for

        Returns
        -------
        pandas.DataFrame
        """
        return self._features

    @classmethod
    def _check_layers(cls, cost_fpath):
        """
        Check to make sure the REQUIRED_LAYERS are in cost_fpath

        Parameters
        ----------
        cost_fpath : str
            Path to h5 file with cost rasters and other required layers
        """
        with ExclusionLayers(cost_fpath) as f:
            missing = []
            for lyr in cls.REQUIRED_LAYRES:
                if lyr not in f:
                    missing.append(lyr)

            if missing:
                msg = ("The following layers are required to compute Least "
                       "Cost Transmission but are missing from {}:\n{}"
                       .format(cost_fpath, missing))
                logger.error(msg)
                raise RuntimeError(msg)

    @classmethod
    def _map_to_costs(cls, cost_fpath, features):
        """
        Map features to cost arrays

        Parameters
        ----------
        cost_fpath : str
            Path to h5 file with cost rasters and other required layers
        features : gpd.GeoDataFrame
            GeoDataFrame of features to connect

        Returns
        -------
        features : gpd.GeoDataFrame
            Table of features to compute LeastCostPaths for
        shape : tuple
            Full cost raster shape
        """
        with ExclusionLayers(cost_fpath) as f:
            crs = CRS.from_string(f.crs)
            cost_crs = crs.to_dict()
            transform = rasterio.Affine(*f.profile['transform'])
            shape = f.shape

        feat_crs = features.crs.to_dict()
        bad_crs = ExclusionsConverter._check_crs(cost_crs, feat_crs)
        if bad_crs:
            logger.warning('input crs ({}) does not match cost raster crs ({})'
                           ' and will be transformed!'
                           .format(feat_crs, cost_crs))
            features = features.to_crs(crs)

        logger.debug('Map features to cost raster')
        coords = features['geometry'].centroid
        row, col = rasterio.transform.rowcol(transform, coords.x.values,
                                             coords.y.values)
        row = np.array(row)
        col = np.array(col)

        # Remove features outside of the cost domain
        mask = row >= 0
        mask &= row < shape[0]
        mask &= col >= 0
        mask &= col < shape[1]

        if any(~mask):
            msg = ("The following features are outside of the cost exclusion "
                   "domain and will be dropped:\n{}"
                   .format(features.loc[~mask]))
            logger.warning(msg)
            row = row[mask]
            col = col[mask]
            features = features.loc[mask].reset_index(drop=True)

        features['row'] = row
        features['col'] = col

        return features, shape

    def _get_clip_slice(self, feature_indices):
        """
        Clip cost raster to bounds of features

        Parameters
        ----------
        feature_indices : ndarray
            (row, col) indices of features

        Returns
        -------
        row_slice : slice
            Row slice to clip too
        col_slice : slice
            Col slice to clip too
        """
        row_slice = slice(max(feature_indices[:, 0].min() - 1, 0),
                          min(feature_indices[:, 0].max() + 1,
                              self._shape[0]))

        col_slice = slice(max(feature_indices[:, 1].min() - 1, 0),
                          min(feature_indices[:, 1].max() + 1,
                              self._shape[1]))

        return row_slice, col_slice

    def _clip_to_feature(self, start_feature_id):
        """
        Clip costs raster to AOI around starting feature to given radius

        Parameters
        ----------
        start_feature_id : int
            Index of start features

        Returns
        -------
        start_idx : ndarray
            (row, col) indicies of start feature
        end_indices : ndarray
            Array of (row, col) end indices, shifted to clipped cost array if
            needed
        """
        start_idx = \
            self._features.loc[start_feature_id, ['row', 'col']].values
        if len(start_idx.shape) == 2:
            start_idx = start_idx[0]

        end_indices = \
            self._features.drop(row=start_feature_id)[['row', 'col']].values

        return start_idx, end_indices

    def process_least_cost_paths(self, capacity_class, barrier_mult=100,
                                 max_workers=None, save_paths=False,):
        """
        Find Least Cost Paths between all pairs of provided features for the
        given tie-line capacity class

        Parameters
        ----------
        capacity_class : str | int
            Capacity class of transmission features to connect supply curve
            points to
        barrier_mult : int, optional
            Tranmission barrier multiplier, used when computing the least
            cost tie-line path, by default 100
        max_workers : int, optional
            Number of workers to use for processing, if 1 run in serial,
            if None use all available cores, by default None
        save_paths : bool, optional
            Flag to save least cost path as a multi-line geometry,
            by default False

        Returns
        -------
        least_cost_paths : pandas.DataFrame | gpd.GeoDataFrame
            DataFrame of lenghts and costs for each path or GeoDataFrame of
            lenght, cost, and geometry for each path
        """
        max_workers = os.cpu_count() if max_workers is None else max_workers
        row_slice, col_slice = \
            self._get_clip_slice(self.features[['row', 'col']].values)

        least_cost_paths = []
        if max_workers > 1:
            logger.info('Computing Least Cost Paths in parallel on {} workers'
                        .format(max_workers))
            loggers = [__name__, 'reV', 'reVX']
            with SpawnProcessPool(max_workers=max_workers,
                                  loggers=loggers) as exe:
                futures = {}
                for start in self.features.index:
                    start_idx, end_indices = \
                        self._clip_to_feature(start_feature_id=start)

                    future = exe.submit(TieLineCosts.run,
                                        self._cost_fpath,
                                        start_idx, end_indices,
                                        capacity_class,
                                        row_slice, col_slice,
                                        barrier_mult=barrier_mult,
                                        min_line_length=0.9,
                                        save_paths=save_paths)
                    futures[future] = start

                for i, future in enumerate(as_completed(futures)):
                    logger.debug('Least cost path {} of {} complete!'
                                 .format(i + 1, len(futures)))
                    lcp = future.result()
                    lcp['start_feature'] = futures[future]
                    least_cost_paths.append(lcp)
        else:
            logger.info('Computing Least Cost Paths in serial')
            i = 1
            for start in self.features.index:
                start_idx, end_indices = \
                    self._clip_to_feature(start_feature_id=start)

                lcp = TieLineCosts.run(self._cost_fpath,
                                       start_idx, end_indices,
                                       capacity_class,
                                       row_slice, col_slice,
                                       barrier_mult=barrier_mult,
                                       min_line_length=0.9,
                                       save_paths=save_paths)
                lcp['start_feature'] = start
                least_cost_paths.append(lcp)

                logger.debug('Least cost path {} of {} complete!'
                             .format(i, len(self.features)))
                i += 1

        least_cost_paths = pd.concat(least_cost_paths)

        return least_cost_paths.reset_index(drop=True)

    @classmethod
    def run(cls, cost_fpath, features_fpath, capacity_class,
            xmission_config=None, barrier_mult=100, max_workers=None,
            save_paths=False):
        """
        Find Least Cost Paths between all pairs of provided features for the
        given tie-line capacity class

        Parameters
        ----------
        cost_fpath : str
            Path to h5 file with cost rasters and other required layers
        features_fpath : str
            Path to geopackage with transmission features
        capacity_class : str | int
            Capacity class of transmission features to connect supply curve
            points to
        xmission_config : str | dict | XmissionConfig, optional
            Path to Xmission config .json, dictionary of Xmission config
            .jsons, or preloaded XmissionConfig objects, by default None
        barrier_mult : int, optional
            Tranmission barrier multiplier, used when computing the least
            cost tie-line path, by default 100
        max_workers : int, optional
            Number of workers to use for processing, if 1 run in serial,
            if None use all available cores, by default None
        save_paths : bool, optional
            Flag to save least cost path as a multi-line geometry,
            by default False

        Returns
        -------
        least_cost_paths : pandas.DataFrame | gpd.GeoDataFrame
            DataFrame of lenghts and costs for each path or GeoDataFrame of
            lenght, cost, and geometry for each path
        """
        ts = time.time()
        lcp = cls(cost_fpath, features_fpath, xmission_config=xmission_config)
        least_cost_paths = lcp.process_least_cost_paths(
            capacity_class,
            barrier_mult=barrier_mult,
            save_paths=save_paths,
            max_workers=max_workers)

        logger.info('{} paths were computed in {:.4f} minutes'
                    .format(len(least_cost_paths),
                            (time.time() - ts) / 60))

        return least_cost_paths
