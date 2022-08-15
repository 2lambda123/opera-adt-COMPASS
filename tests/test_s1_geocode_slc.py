import numpy as np
import numpy.testing as npt
from osgeo import gdal

from compass import s1_geocode_slc
from compass.utils.geo_runconfig import GeoRunConfig


def gdal_get_arr(f):
    ds = gdal.Open(f, gdal.GA_ReadOnly)
    arr = np.array([])
    if ds is not None:
        arr = ds.GetRasterBand(1).ReadAsArray()
    return arr

def test_geocode_slc_run(test_paths):

    # load yaml to cfg
    cfg = GeoRunConfig.load_from_yaml(test_paths.gslc_cfg_path,
                                      workflow_name='s1_cslc_geo')

    # pass cfg to s1_geocode_slc
    s1_geocode_slc.run(cfg)

def test_geocode_slc_validate(test_paths):
    # load test output
    test_arr = gdal_get_arr(test_paths.test_gslc)

    # load reference output
    ref_arr = gdal_get_arr(test_paths.ref_gslc)

    npt.assert_array_equal(test_arr, ref_arr)
