'''collection of useful functions used across workflows'''

import os
import sqlite3

import isce3
import journal
import numpy as np
from pyproj.transformer import Transformer
from osgeo import gdal
from shapely import geometry

import compass


WORKFLOW_SCRIPTS_DIR = os.path.dirname(compass.__file__)

# get the basename given an input file path
# example: get_module_name(__file__)
get_module_name = lambda x : os.path.basename(x).split('.')[0]


def check_file_path(file_path: str) -> None:
    """Check if file_path exist else raise an error.

    Parameters
    ----------
    file_path : str
        Path to file to be checked
    """
    error_channel = journal.error('helpers.check_file_path')
    if not os.path.exists(file_path):
        err_str = f'{file_path} not found'
        error_channel.log(err_str)
        raise FileNotFoundError(err_str)


def check_directory(file_path: str) -> None:
    """Check if directory in file_path exists else raise an error.

    Parameters
    ----------
    file_path: str
       Path to directory to be checked
    """
    error_channel = journal.error('helpers.check_directory')
    if not os.path.isdir(file_path):
        err_str = f'{file_path} not found'
        error_channel.log(err_str)
        raise FileNotFoundError(err_str)

def get_file_polarization_mode(file_path: str) -> str:
    '''Check polarization mode from file name

    Taking PP from SAFE file name with following format:
    MMM_BB_TTTR_LFPP_YYYYMMDDTHHMMSS_YYYYMMDDTHHMMSS_OOOOOO_DDDDDD_CCCC.SAFE

    Parameters
    ----------
    file_path : str
        SAFE file name to parse

    Returns
    -------
    original: dict
        Default dictionary updated with user-defined options

    References
    ----------
    https://sentinel.esa.int/web/sentinel/user-guides/sentinel-1-sar/naming-conventions
    '''
    # index split tokens from rear to account for R in TTTR being possibly
    # replaced with '_'
    safe_pol_mode = os.path.basename(file_path).split('_')[-6][2:]

    return safe_pol_mode


def deep_update(original, update):
    """Update default runconfig dict with user-supplied dict.

    Parameters
    ----------
    original : dict
        Dict with default options to be updated
    update: dict
        Dict with user-defined options used to update original/default

    Returns
    -------
    original: dict
        Default dictionary updated with user-defined options

    References
    ----------
    https://stackoverflow.com/questions/3232943/update-value-of-a-nested-dictionary-of-varying-depth
    """
    for key, val in update.items():
        if isinstance(val, dict):
            original[key] = deep_update(original.get(key, {}), val)
        else:
            original[key] = val

    # return updated original
    return original


def check_write_dir(dst_path: str):
    """Check if given directory is writeable; else raise error.

    Parameters
    ----------
    dst_path : str
        File path to directory for which to check writing permission
    """
    if not dst_path:
        dst_path = '.'

    error_channel = journal.error('helpers.check_write_dir')

    # check if scratch path exists
    dst_path_ok = os.path.isdir(dst_path)

    if not dst_path_ok:
        try:
            os.makedirs(dst_path, exist_ok=True)
        except OSError:
            err_str = f"Unable to create {dst_path}"
            error_channel.log(err_str)
            raise OSError(err_str)

    # check if path writeable
    write_ok = os.access(dst_path, os.W_OK)
    if not write_ok:
        err_str = f"{dst_path} scratch directory lacks write permission."
        error_channel.log(err_str)
        raise PermissionError(err_str)


def check_dem(dem_path: str):
    """Check if given path is a GDAL-compatible file; else raise error

    Parameters
    ----------
    dem_path : str
        File path to DEM for which to check GDAL-compatibility
    """
    error_channel = journal.error('helpers.check_dem')
    try:
        gdal.Open(dem_path, gdal.GA_ReadOnly)
    except:
        err_str = f'{dem_path} cannot be opened by GDAL'
        error_channel.log(err_str)
        raise ValueError(err_str)

    epsg = isce3.io.Raster(dem_path).get_epsg()
    if not 1024 <= epsg <= 32767:
        err_str = f'DEM epsg of {epsg} out of bounds'
        error_channel.log(err_str)
        raise ValueError(err_str)


def bbox_to_utm(bbox, *, epsg_src, epsg_dst):
    """Convert bounding box coordinates to UTM.

    Parameters
    ----------
    bbox : tuple
        Tuple containing the lon/lat bounding box coordinates
        (left, bottom, right, top) in degrees
    epsg_src : int
        EPSG code identifying input bbox coordinate system
    epsg_dst : int
        EPSG code identifying output coordinate system

    Returns
    -------
    tuple
        Tuple containing the bounding box coordinates in UTM (meters)
        (left, bottom, right, top)
    """
    xmin, ymin, xmax, ymax = bbox
    xys = _convert_to_utm([(xmin, ymin), (xmax, ymax)], epsg_src, epsg_dst)
    return (*xys[0], *xys[1])


def polygon_to_utm(poly, *, epsg_src, epsg_dst):
    """Convert a shapely.Polygon's coordinates to UTM.

    Parameters
    ----------
    poly: shapely.geometry.Polygon
        Polygon object
    epsg : int
        EPSG code identifying output projection system

    Returns
    -------
    tuple
        Tuple containing the bounding box coordinates in UTM (meters)
        (left, bottom, right, top)
    """
    coords = np.array(poly.exterior.coords)
    xys = _convert_to_utm(coords, epsg_src, epsg_dst)
    return geometry.Polygon(xys)


def _convert_to_utm(points_xy, epsg_src, epsg_dst):
    """Convert a list of points to a specified UTM coordinate system.

    If epsg_src is 4326 (lat/lon), assumes points_xy are in degrees.
    """
    if epsg_dst == epsg_src:
        return points_xy

    t = Transformer.from_crs(epsg_src, epsg_dst, always_xy=True)
    xs, ys = np.array(points_xy).T
    xt, yt = t.transform(xs, ys)
    return list(zip(xt, yt))


def get_burst_bbox(burst_id, burst_db_file=None, burst_db_conn=None):
    """Find the bounding box of a burst (or bursts) in the database.

    Can either pass one string burst_id or a list of burst_ids.

    Parameters
    ----------
    burst_id : str or list[str]
        JPL burst ID, or a list of burst IDs.
    burst_db_file : str
        Location of burst database sqlite file, by default None
    burst_db_conn : sqlite3.Connection
        Connection object to burst database (If already connected)
        Alternative to providing burst_db_file, will be faster
        for multiply queries.

    Returns
    -------
    epsg : int, or list[int]
        EPSG code (or codes) of burst bounding box(es)
    bbox : tuple[float] or list[tuple[float]]
        Bounding box of burst in EPSG coordinates, or list of bounding boxes.

    Raises
    ------
    ValueError
        If no burst_id is not found in burst database
    """
    # example burst db:
    # /home/staniewi/dev/burst_map_IW_000001_375887.OPERA-JPL.sqlite3
    if burst_db_conn is None:
        burst_db_conn = sqlite3.connect(burst_db_file)
    burst_db_conn.row_factory = sqlite3.Row  # return rows as dicts

    burst_ids = [burst_id] if isinstance(burst_id, str) else burst_id

    results = []
    query = "SELECT epsg, xmin, ymin, xmax, ymax FROM burst_id_map WHERE burst_id_jpl = ?"
    for bid in burst_ids:
        cur = burst_db_conn.execute(query, (bid,))
        results.append(cur.fetchone())

    if not results:
        raise ValueError(f"Failed to find {burst_ids} in {burst_db_file}")

    # If they only requested one, just return the single epsg/bbox
    if len(results) == 1:
        result = results[0]
        epsg = result["epsg"]
        bbox = (result["xmin"], result["ymin"], result["xmax"], result["ymax"])
        return epsg, bbox

    # Otherwise, return a list of epsg/bbox tuples
    epsgs = [r["epsg"] for r in results]
    bboxes = [(r["xmin"], r["ymin"], r["xmax"], r["ymax"]) for r in results]
    return epsgs, bboxes


def save_rdr_burst(bursts, scratch):
    burst_id = bursts[0].burst_id
    if len(bursts) > 1:
        burst_paths = []
        for pol_burst in bursts:
            pol = pol_burst.polarization
            temp_path = f'{scratch}/{burst_id}_{pol}_temp.vrt'
            burst_paths.append(temp_path)
            pol_burst.slc_to_vrt_file(temp_path)

        burst_rdr_path = f'{scratch}/{burst_id}_temp.tiff'
        in_ds = gdal.Open(burst_paths[0], gdal.GA_ReadOnly)
        driver = gdal.GetDriverByName('GTiff')
        length, width = in_ds.RasterYSize, in_ds.RasterXSize
        out_ds = driver.Create(burst_rdr_path, width, length,
                               len(bursts), gdal.GDT_CFloat32)

        for k in range(len(burst_paths)):
            in_ds = gdal.Open(burst_paths[k], gdal.GA_ReadOnly)
            burst_data = in_ds.GetRasterBand(1).ReadAsArray()
            out_ds.GetRasterBand(k+1).WriteArray(burst_data)
        out_ds.FlushCache()
        out_ds = None

    else:
        pol = bursts[0].polarization
        burst_rdr_path = f'{scratch}/{burst_id}_{pol}_temp.vrt'
        bursts[0].slc_to_vrt_file(burst_rdr_path)
    return burst_rdr_path
