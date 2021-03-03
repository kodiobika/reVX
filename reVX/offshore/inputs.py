# -*- coding: utf-8 -*-
"""
Extract offshore inputs from exclusion layers
"""
import logging
import numpy as np
import pandas as pd
from scipy.ndimage import center_of_mass

from rex.resource import Resource
from rex.utilities.utilities import parse_table
from reV.handlers.exclusions import ExclusionLayers

logger = logging.getLogger(__name__)


class OffshoreInputs:
    """
    Class to extract offshore inputs from offshore inputs .h5
    """
    def __init__(self, inputs_fpath, offshore_sites, tm_dset='techmap_wtk'):
        self._inputs_fpath = inputs_fpath
        self._offshore_meta = self._create_offshore_meta(offshore_sites,
                                                         tm_dset)

    @staticmethod
    def _reduce_tech_map(inputs_fpath, tm_dset='techmap_wtk',
                         offshore_gids=None):
        """
        Find the row and column indices that correspond to the centriod of
        each offshore gid in exclusions layers. If offshore gids are not
        provided the centroid of every gid is in techmap.

        Parameters
        ----------
        inputs_fpath : str
            Path to offshore inputs .h5 file
        tm_dset : str, optional
            Dataset / layer name for wind toolkit techmap,
            by default 'techmap_wtk'
        offshore_gids : ndarray | list, optional
            Vector or list of offshore gids, by default None

        Returns
        -------
        tech_map : pandas.DataFrame
            DataFrame mapping resource gid to exclusions latitude, longitude,
            row index, column index
        """
        with ExclusionLayers(inputs_fpath) as f:
            tech_map = f[tm_dset]

        if offshore_gids is None:
            offshore_gids = np.unique(tech_map)
            offshore_gids = offshore_gids[offshore_gids >= 0]

        tech_map = np.array(center_of_mass(tech_map, labels=tech_map,
                                           index=offshore_gids),
                            dtype='float32')

        tech_map = pd.DataFrame(tech_map, columns=['row_id', 'col_id'])
        tech_map['gid'] = offshore_gids

        return tech_map

    @staticmethod
    def _parse_offshore_sites(offshore_sites):
        """
        Load offshore sites from disc if needed

        Parameters
        ----------
        offshore_sites : str | list | tuple | ndarray |pandas.DataFrame
            - Path to .csv|.json file with offshore sites meta data
            - Path to a WIND Toolkit .h5 file to extact site meta from
            - List, tuple, or vector of offshore gids
            - Pre-extracted site meta DataFrame

        Returns
        -------
        offshore_sites : pandas.DataFrame
            Offshore sites meta data
        """
        if isinstance(offshore_sites, str):
            if offshore_sites.endswith('.h5'):
                with Resource(offshore_sites) as f:
                    offshore_sites = f.meta.reset_index()
            else:
                offshore_sites = parse_table(offshore_sites)
        elif isinstance(offshore_sites, (tuple, list, np.ndarray)):
            offshore_sites = pd.DataFrame({'gid': offshore_sites})

        if not isinstance(offshore_sites, pd.DataFrame):
            msg = ("offshore sites must be a .csv, .json, or .h5 file path, "
                   "or a pre-extracted pandas DataFrame, but {} was provided"
                   .format(offshore_sites))
            logger.error(msg)
            raise ValueError(msg)

        if 'offshore' in offshore_sites:
            mask = offshore_sites['offshore'] == 1
            offshore_sites = offshore_sites.loc[mask]

        return offshore_sites

    def _create_offshore_meta(self, offshore_sites, tm_dset='techmap_wtk'):
        """
        Create offshore meta from offshore sites and techmap

        Parameters
        ----------
        offshore_sites : str | pandas.DataFrame
            Path to .csv file with offshore sites or offshore meta, or path
            to a .h5 file to extact site meta from, or pre-extracted site meta
            DataFrame
        tm_dset : str, optional
            Dataset / layer name for wind toolkit techmap,
            by default 'techmap_wtk'

        Returns
        -------
        offshore_meta : pandas.DataFrames
            Offshore sites meta data including mapping to input layers
        """
        offshore_sites = self._parse_offshore_sites(offshore_sites)
        if 'gid' not in offshore_sites:
            msg = ('Cannot find offshore WIND Toolkit "gid"s of interest! '
                   'Offshore sites input must have a "gid" column: {}'
                   .format(list(offshore_sites.columns)))
            logger.error(msg)
            raise RuntimeError(msg)

        offshore_gids = offshore_sites['gid'].values
        tech_map = self._reduce_tech_map(self._inputs_fpath, tm_dset=tm_dset,
                                         offshore_gids=offshore_gids)

        offshore_meta = pd.merge(offshore_sites, tech_map, on='gid')

        return offshore_meta
