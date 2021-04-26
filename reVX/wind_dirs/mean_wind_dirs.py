# -*- coding: utf-8 -*-
"""
Compute the mean wind direction for each supply curve point
"""
import logging

from reV.supply_curve.aggregation import Aggregation
from reVX.utilities.utilities import log_versions

logger = logging.getLogger(__name__)


class MeanWindDirections(Aggregation):
    """
    Average the wind direction via the wind vectors.
    Then convert to equivalent sc_point_gid
    """

    def __init__(self, res_h5_fpath, excl_fpath, wdir_dsets,
                 tm_dset='techmap_wtk', excl_dict=None,
                 area_filter_kernel='queen', min_area=None,
                 resolution=128, excl_area=None):
        """
        Parameters
        ----------
        res_h5_fpath : str
            Filepath to .h5 file containing wind direction data
        excl_fpath : str
            Filepath to exclusions h5 with techmap dataset.
        wdir_dsets : str | list
            Wind direction dataset to average
        tm_dset : str, optional
            Dataset name in the techmap file containing the
            exclusions-to-resource mapping data,
            by default 'techmap_wtk'
        excl_dict : dict | None, optional
            Dictionary of exclusion LayerMask arugments {layer: {kwarg: value}}
            by default None
        area_filter_kernel : str, optional
            Contiguous area filter method to use on final exclusions mask,
            by default 'queen'
        min_area : float | None, optional
            Minimum required contiguous area filter in sq-km, by default None
        resolution : int | None, optional
            SC resolution, must be input in combination with gid,
            by default 128
        excl_area : float | None, optional
            Area of an exclusion pixel in km2. None will try to infer the area
            from the profile transform attribute in excl_fpath,
            by default None
        """
        log_versions(logger)
        if isinstance(wdir_dsets, str):
            wdir_dsets = [wdir_dsets]

        for dset in wdir_dsets:
            if not dset.startswith('winddirection'):
                msg = ('{} is not a valid wind direction dataset!'
                       .format(dset))
                logger.error(msg)
                raise ValueError(msg)

        super().__init__(excl_fpath, res_h5_fpath, tm_dset, *wdir_dsets,
                         excl_dict=excl_dict,
                         area_filter_kernel=area_filter_kernel,
                         min_area=min_area,
                         resolution=resolution, excl_area=excl_area)

    def aggregate(self, max_workers=None, sites_per_worker=1000):
        """
        Average wind directions to sc_points

        Parameters
        ----------
        max_workers : int | None
            Number of cores to run summary on. None is all
            available cpus.
        sites_per_worker : int, optional
            Number of SC points to process on a single parallel worker,
            by default 1000

        Returns
        -------
        agg : dict
            Aggregated values for each aggregation dataset
        """
        agg = super().aggregate(agg_method='mean_wind_dir',
                                max_workers=max_workers,
                                sites_per_worker=sites_per_worker)

        return agg

    @classmethod
    def run(cls, res_h5_fpath, excl_fpath, wdir_dsets,
            tm_dset='techmap_wtk', excl_dict=None,
            area_filter_kernel='queen', min_area=None,
            resolution=128, excl_area=None,
            max_workers=None, sites_per_worker=1000, out_fpath=None):
        """
        Aggregate powerrose to supply curve points, find neighboring supply
        curve point gids and rank them based on prominent powerrose direction

        Parameters
        ----------
        res_h5_fpath : str
            Filepath to .h5 file containing wind direction data
        excl_fpath : str
            Filepath to exclusions h5 with techmap dataset.
        wdir_dsets : str | list
            Wind direction dataset to average
        tm_dset : str, optional
            Dataset name in the techmap file containing the
            exclusions-to-resource mapping data,
            by default 'techmap_wtk'
        excl_dict : dict | None, optional
            Dictionary of exclusion LayerMask arugments {layer: {kwarg: value}}
            by default None
        area_filter_kernel : str, optional
            Contiguous area filter method to use on final exclusions mask,
            by default 'queen'
        min_area : float | None, optional
            Minimum required contiguous area filter in sq-km, by default None
        resolution : int | None, optional
            SC resolution, must be input in combination with gid,
            by default 128
        excl_area : float | None, optional
            Area of an exclusion pixel in km2. None will try to infer the area
            from the profile transform attribute in excl_fpath,
            by default None
        max_workers : int | None, optional
            Number of cores to run summary on. None is all
            available cpus, by default None
        sites_per_worker : int, optional
            Number of SC points to process on a single parallel worker,
            by default 1000
        out_fpath : str
            Path to .h5 file to save aggregated data too

        Returns
        -------
        agg : dict
            Aggregated values for each aggregation dataset
        """
        wdir = cls(res_h5_fpath, excl_fpath, wdir_dsets, tm_dset=tm_dset,
                   excl_dict=excl_dict,
                   area_filter_kernel=area_filter_kernel,
                   min_area=min_area,
                   resolution=resolution, excl_area=excl_area)

        agg = wdir.aggregate(max_workers=max_workers,
                             sites_per_worker=sites_per_worker)

        if out_fpath is not None:
            wdir.save_agg_to_h5(out_fpath, agg)

        return agg
