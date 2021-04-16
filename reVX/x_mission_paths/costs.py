"""
Calculate least cost paths from supply curve points to transmission features

Mike Bannister
4/13/2021
"""
import numpy as np
import matplotlib.pyplot as plt

from skimage.graph import MCP_Geometric
from collections import namedtuple

from shapely.geometry import Point
from shapely.ops import nearest_points


class SupplyCurvePoint:
    def __init__(self, id, x, y, row, col):
        """
        Parameters
        ----------
        id : int
            Id of supply curve point
        x : float
            Projected easting coordinate
        y : float
            Projected northing coordinate
        row : int
            Row in template raster that corresponds to y
        col : int
            Column in template raster that corresponds to x
        """
        self.id = id
        self.x = x
        self.y = y
        self.row = row
        self.col = col

    @property
    def point(self):
        """
        Return point as shapley.geometry.Point object

        """
        return Point(self.x, self.y)

    def __repr__(self):
        return f'id={self.id}, coords=({self.x}, {self.y}), ' +\
               f'r/c=({self.row}, {self.col})'


class TransFeature:
    """ Represents a substation, t-line, etc """
    def __init__(self, id, name, trans_type, x, y, row, col, dist):
        """
        Parameters
        ----------
        id : int
            Id of transmission feature
        name : str
            Name of feature
        trans_type : str
            Type of transmission feature, e.g. 'subs', 't-line', etc.
        x : float
            Projected easting coordinate
        y : float
            Projected northing coordinate
        row : int
            Row in template raster that corresponds to y
        col : int
            Column in template raster that corresponds to x
        dist : float
            Straight line distance from feature to supply curve point, in
            projected units.
        """
        self.id = id
        self.name = name
        self.trans_type = trans_type
        self.x = x
        self.y = y
        self.row = row
        self.col = col
        self.dist = dist

    def __repr__(self):
        return f'id={self.id}, coords=({self.x}, {self.y}), ' +\
               f'r/c=({self.row}, {self.col}), dist={self.dist}, ' +\
               f'name={self.name}, type={self.trans_type}'


# TODO - does this include substation attachemnt cost?
TransmissionCost = namedtuple('TransmissionCost',
                              'sc_id trans_id name trans_type cost length')
TransmissionCost.__doc__ = """
Cost of building transmission line from supply curve point to transmission
feature.

Parameters
----------
sc_id : int
    Supply curve point id
trans_id : int
    Supply curve point id
name : str
    Name of transmission feature
trans_type : str
    Type of transmission feature, e.g. 'subs', 't-line', etc.
cost : float
    Cost of building t-line from supply curve point to trans feature
length : float
    Minimum cost path length in meters of new line
"""


class SubstationDistanceCalculator:
    """
    Calculate nearest substations to SC point. Also calculate distance and
    row/col in template raster.
    """
    def __init__(self, subs, rct, n=10):
        """
        Parameters
        ----------
        subs : geopandas.DataFrame
            Substations to search
        rct : RowColTransformer
            Transformer for template raster
        n : int
            Number of nearest t-lines to return
        """
        self._subs = subs
        self._rct = rct
        self._n = n

    def get_closest(self, sc_pt):
        """
        Get n closest substations to a supply curve point

        Parameters
        ----------
        sc_pt : SupplyCurvePoint
            Supply curve point to search around

        Returns
        -------
        close_subs : list
            List of n nearest substations to location
        """
        # Get shapely point for geometry calcs
        pt = sc_pt.point

        # Find nearest subs to sc_pt
        self._subs['dist'] = self._subs.distance(pt)
        subs = self._subs.sort_values(by='dist')
        near_subs = subs[:self._n].copy()

        # Determine row/col and convert to TransFeature
        close_subs = []
        for _id, sub in near_subs.iterrows():
            row, col = self._rct.get_row_col(sub.geometry.x, sub.geometry.y)
            if row is None:
                continue
            new_sub = TransFeature(_id, sub.Name, 'sub', sub.geometry.x,
                                   sub.geometry.y, row, col, sub.dist)
            close_subs.append(new_sub)
        return close_subs


class TLineDistanceCalculator:
    """
    Calculate nearest t-lines to SC point. Also calculate distance and
    row/col in template raster.
    """
    def __init__(self, tls, rct, n=10):
        """
        Parameters
        ----------
        tls : geopandas.DataFrame
            Transmission lines to search
        rct : RowColTransformer
            Transformer for template raster
        n : int
            Number of nearest t-lines to return
        """
        self._tls = tls
        self._rct = rct
        self._n = n

    def get_closest(self, sc_pt):
        """
        Get n closest t-lines to a supply curve point

        Parameters
        ----------
        sc_pt : SupplyCurvePoint
            Supply curve point to search around

        Returns
        -------
        close_tls : list
            List of n nearest t-lines to location
        """
        # Get shapely point for geometry calcs
        pt = sc_pt.point

        # Find nearest t-lines to sc_pt
        self._tls['dist'] = self._tls.distance(pt)
        tls = self._tls.sort_values(by='dist')
        near_tls = tls[:self._n].copy()

        # Determine row/col of nearest pt on line and convert to TransFeature
        close_tls = []
        for _id, tl in near_tls.iterrows():
            # Find pt on t-line closest to sc
            near_pt, _ = nearest_points(tl.geometry, pt)
            row, col = self._rct.get_row_col(near_pt.x, near_pt.y)
            if row is None:
                continue
            new_tl = TransFeature(_id, tl.Name, 't-line', near_pt.x,
                                  near_pt.y, row, col, tl.dist)
            close_tls.append(new_tl)
        return close_tls


class PathFinder:
    """
    Find least cost paths to transmission features from SC point
    """
    def __init__(self, sc_pt, mults, subs_dc, tls_dc):
        """
        sc_pt : SupplyCurvePoint
            Supply curve point of interest
        mults : numpy.ndarray
            Multiplier raster
        subs_dc : DistanceCalculator
            Distance calculator for substations
        tls_dc : DistanceCalculator
            Distance calculator for t-lines
        """
        self._sc_pt = sc_pt
        self._mults = mults
        self._subs_dc = subs_dc
        self._tls_dc = tls_dc

        self.cell_size = 90  # meters, size of cell. Both dims must be equal

        self._near_trans = None
        self._row_offset = None
        self._col_offset = None
        self._mults_clip = None
        self._costs = None
        self._tb = None

    @classmethod
    def run(cls, sc_pt,  mults, subs_dc, tls_dc):
        pf = cls(sc_pt, mults, subs_dc, tls_dc)
        pf._clip_cost_raster()
        pf._find_paths()
        return pf

    def _clip_cost_raster(self):
        """ Clip cost raster to nearest transmission features """
        subs = self._subs_dc.get_closest(self._sc_pt)
        tls = self._tls_dc.get_closest(self._sc_pt)

        self._near_trans = subs + tls
        self._near_trans.sort(key=lambda x: x.dist)

        rows = [x.row for x in self._near_trans]
        cols = [x.col for x in self._near_trans]
        rows.append(self._sc_pt.row)
        cols.append(self._sc_pt.col)

        self._row_offset = min(rows)
        self._col_offset = min(cols)

        self._mults_clip = self._mults[min(rows):max(rows)+1,
                                       min(cols):max(cols)+1]

    def _find_paths(self):
        """ Find minimum cost paths from sc_pt to nearest trans features """
        self._mcp = MCP_Geometric(self._mults_clip)
        self._costs, self._tb = self._mcp.find_costs(starts=[self._start])

    @property
    def costs(self):
        """
        Return list of costs data

        Returns
        -------
        costs : list of TransmissionCost
            Costs data for minimum cost paths to nearest x-mission features
        """
        assert self._costs is not None, 'Please start class with run()'

        costs = []
        for feat in self._near_trans:
            length = self._path_length(feat)
            cost = self._path_cost(feat)
            this_cost = TransmissionCost(self._sc_pt.id, feat.id, feat.name,
                                         feat.trans_type, cost, length)
            costs.append(this_cost)
        return costs

    @property
    def _start(self):
        """
        Return supply curve point row/col location for clipped mults raster
        """
        start = (self._sc_pt.row - self._row_offset,
                 self._sc_pt.col - self._col_offset)
        return start

    def _path_cost(self, feat):
        r, c = self._feat_row_col(feat)
        return self._costs[r, c]

    def _path_length(self, feat):
        """
        Calculate length of minimum cost path to substation

        Parameters
        ----------
        feat : TransFeature
            Transmission feature of interest

        Returns
        -------
        float : length of minimum cost path in meters
        """
        r, c = self._feat_row_col(feat)
        indices = self._mcp.traceback((r, c))
        apts = np.array(indices)

        # Use phythagorean theorem to calulate lengths between cells
        lengths = np.sqrt(np.sum(np.diff(apts, axis=0)**2, axis=1))
        total_length = np.sum(lengths)
        return total_length * self.cell_size

    def _feat_row_col(self, feat):
        """
        Return feature row and column location on clipped raster

        Parameters
        ----------
        feat : TransFeature
            Feature of interest

        Returns
        -------
        row : int
            Row location on template raster of feature
        col : int
            Column location on template raster of feature
        """
        row = feat.row - self._row_offset
        col = feat.col - self._col_offset
        return row, col

    def plot_paths(self):
        """ Plot least cost paths for QAQC"""
        assert self._tb is not None, 'Must run _find_paths() first'

        plt.figure(figsize=(30, 15))
        plt.imshow(self._mults_clip)

        # Plot substations
        subs = [(x.row, x.col, x) for x in self._near_trans]
        for r, c, sub in subs:
            plt.plot(c - self._col_offset, r - self._row_offset,
                     marker='o', color="red")
            plt.text(c - self._col_offset, r - self._row_offset,
                     sub.name, color='white')

        # Plot paths
        for sub in self._near_trans:
            r, c = self._feat_row_col(sub)
            indices = self._mcp.traceback((r, c))
            path_xs = [x[1] for x in indices]
            path_ys = [x[0] for x in indices]
            plt.plot(path_xs, path_ys, color='white')

        # Plot SC point
        print(f'Plotting start as {self._start}')
        plt.plot(self._start[1], self._start[0],
                 marker='o', color='black', markersize=18)
        plt.plot(self._start[1], self._start[0],
                 marker='o', color='yellow', markersize=10)
