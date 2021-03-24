# -*- coding: utf-8 -*-
"""
Extract offshore inputs from exclusion layers
"""
import logging
import numpy as np
import pandas as pd
from scipy.ndimage import center_of_mass
from scipy.spatial import cKDTree
from warnings import warn

from reV.handlers.exclusions import ExclusionLayers
from reVX.utilities.utilities import log_versions, coordinate_distance
from rex.resource import Resource
from rex.utilities.utilities import parse_table, get_lat_lon_cols

logger = logging.getLogger(__name__)


class OffshoreInputs(ExclusionLayers):
    """
    Class to extract offshore inputs from offshore inputs .h5 at desired
    offshore site gids. Mapping is based on the techmapping dataset (tm_dset).
    Offshore input values are taken from the array pixel closest to the
    center of mass of each offshore site gid.
    """
    DEFAULT_INPUT_LAYERS = {
        'array_efficiency': 'aeff',
        'bathymetry': 'depth',
        'assembly_areas': 'dist_a_to_s',
        'ports_operations': 'dist_op_to_s',
        'ports_construction': 'dist_p_to_s',
        'ports_construction_nolimits': 'dist_p_to_s_nolimit',
        'weather_downtime_fixed_bottom': 'fixed_downtime',
        'weather_downtime_floating': 'floating_downtime',
        # '': 'hs_average'
    }

    def __init__(self, inputs_fpath, offshore_sites, tm_dset='techmap_wtk'):
        """
        Parameters
        ----------
        inputs_fpath : str
            Path to offshore inputs .h5 file
        offshore_sites : str | list | tuple | ndarray |pandas.DataFrame
            - Path to .csv|.json file with offshore sites meta data
            - Path to a WIND Toolkit .h5 file to extact site meta from
            - List, tuple, or vector of offshore gids
            - Pre-extracted site meta DataFrame
        tm_dset : str, optional
            Dataset / layer name for wind toolkit techmap,
            by default 'techmap_wtk'
        """
        log_versions(logger)
        super().__init__(inputs_fpath)
        self._offshore_meta = self._create_offshore_meta(offshore_sites,
                                                         tm_dset)

    def __repr__(self):
        msg = "{} from {}".format(self.__class__.__name__, self.inputs_fpath)

        return msg

    @property
    def inputs_fpath(self):
        """
        .h5 file containing offshore input layers

        Returns
        -------
        str
        """
        return self.h5_file

    @property
    def meta(self):
        """
        Offshore site meta data including mapping to input layer row and column
        index

        Returns
        -------
        pandas.DataFrame
        """
        return self._offshore_meta

    @property
    def lat_lons(self):
        """
        Offshore sites coordinates (lat, lons)

        Returns
        -------
        ndarray
        """
        lat_lon_cols = get_lat_lon_cols(self.meta)

        return self.meta[lat_lon_cols].values

    @property
    def row_ids(self):
        """
        Input layer array row ids that correspond to desired offshore sites

        Returns
        -------
        ndarray
        """
        return self.meta['row_idx'].values

    @property
    def column_ids(self):
        """
        Input layer array column ids that correspond to desired offshore sites

        Returns
        -------
        ndarray
        """
        return self.meta['col_idx'].values

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
                    offshore_sites = f.meta
                    if offshore_sites.index.name == 'gid':
                        offshore_sites = offshore_sites.reset_index()

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

    def _reduce_tech_map(self, tm_dset='techmap_wtk', offshore_gids=None):
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
        tech_map = self[tm_dset]

        gids = np.unique(tech_map)

        if offshore_gids is None:
            offshore_gids = gids[gids >= 0]
        else:
            missing = ~np.isin(offshore_gids, gids)
            if np.any(missing):
                msg = ('The following offshore gids were requested but are '
                       'not availabe in {} and will not be extracted:\n{}'
                       .format(tm_dset, offshore_gids[missing]))
                logger.warning(msg)
                warn(msg)
                offshore_gids = offshore_gids[~missing]

        tech_map = np.array(center_of_mass(tech_map, labels=tech_map,
                                           index=offshore_gids),
                            dtype=np.uint32)

        tech_map = pd.DataFrame(tech_map, columns=['row_idx', 'col_idx'])
        tech_map['gid'] = offshore_gids

        return tech_map

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
        offshore_meta : pandas.DataFrame
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
        tech_map = self._reduce_tech_map(tm_dset=tm_dset,
                                         offshore_gids=offshore_gids)

        offshore_meta = pd.merge(offshore_sites, tech_map, on='gid',
                                 how='inner')

        return offshore_meta

    def compute_assembly_dist(self, layer):
        """
        Extract the distance from ports to assembly area and then compute
        the distance from nearest assembly area to sites

        Parameters
        ----------
        layer : str
            Name of assembly area table/dataset

        Returns
        -------
        out : dict
            Dictionary containing the distance from ports to assembly areas
            ('dist_p_to_a') and distance from nearest assembly area to sites
            ('dist_a_to_s')
        """
        df = pd.DataFrame(self.h5[layer])
        df = self.h5.df_str_decode(df)
        # pylint: disable = not-callable
        lat_lon_cols = get_lat_lon_cols(df)
        tree = cKDTree(df[lat_lon_cols].values)

        site_lat_lons = self.lat_lons
        _, pos = tree.query(self.lat_lons)

        out = {}
        # extract distance from ports to assembly areas
        df = df.iloc[pos]
        out['dist_p_to_a'] = df['dist_p_to_a'].values

        # compute distance from assembly areas to sites
        out['dist_a_to_s'] = coordinate_distance(site_lat_lons,
                                                 df[lat_lon_cols].values)

        return out

    def extract_input_layer(self, layer):
        """
        Extract input data for desired layer

        Parameters
        ----------
        layer : str
            Desired input layer

        Returns
        -------
        data : ndarray
            Input layer data for desired offshore sites
        """
        data = self[layer, self.row_ids, self.column_ids]

        return data

    def get_offshore_inputs(self, input_layers=None):
        """
        Extract data for the desired layers

        Parameters
        ----------
        layers : str | list | dict
            Input layer, list of input layers, to extract, or dictionary
            mapping the input layers to extract to the column names to save
            them under

        Returns
        -------
        out : pandas.DataFrame
            Updated meta data table with desired layers
        """
        msg = ''
        if input_layers is None:
            input_layers = self.DEFAULT_INPUT_LAYERS
            msg += '"input_layers" not provided, using defaults. '
        else:
            if isinstance(input_layers, str):
                input_layers = [input_layers]

            if isinstance(input_layers, (tuple, list, np.ndarray)):
                input_layers = {layer: layer for layer in input_layers}

        if not isinstance(input_layers, dict):
            msg = ('Expecting "layers" to be a the name of a single input '
                   'layer, a list of input layers, or a dictionary mapping '
                   'desired input layers to desired output column names, but '
                   'recieved: {}'.format(type(input_layers)))
            logger.error(msg)
            raise TypeError(msg)

        msg += 'Extracting {}'.format(input_layers)
        logger.info(msg)

        out = self.meta.copy()
        for layer, col in input_layers.items():
            if layer not in self:
                msg = ("{} is not a valid offshore input layers, please "
                       "choice one of: {}".format(layer, self.layers))
                logger.error(msg)
                raise KeyError(msg)

            if layer.startswith('assembly'):
                for col, data in self.compute_assembly_dist(layer).items():
                    out[col] = data
            else:
                out[col] = self.extract_input_layer(layer)

        return out

    @classmethod
    def extract(cls, inputs_fpath, offshore_sites, input_layers=None,
                tm_dset='techmap_wtk', out_fpath=None):
        """
        Extract data from desired input layers for desired offshore sites

        Parameters
        ----------
        inputs_fpath : str
            Path to offshore inputs .h5 file
        offshore_sites : str | list | tuple | ndarray |pandas.DataFrame
            - Path to .csv|.json file with offshore sites meta data
            - Path to a WIND Toolkit .h5 file to extact site meta from
            - List, tuple, or vector of offshore gids
            - Pre-extracted site meta DataFrame
        input_layers : str | list | dict
            Input layer, list of input layers, to extract, or dictionary
            mapping the input layers to extract to the column names to save
            them under
        tm_dset : str, optional
            Dataset / layer name for wind toolkit techmap,
            by default 'techmap_wtk'
        out_fpath : str, optional
            Output .csv path to save offshore inputs too, by default None

        Returns
        -------
        out : pandas.DataFrame
            Updated meta data table with desired layers
        """
        with cls(inputs_fpath, offshore_sites, tm_dset=tm_dset) as off_ipt:
            out = off_ipt.get_offshore_inputs(input_layers=input_layers)

        if out_fpath:
            out.to_csv(out_fpath, index=False)

        return out