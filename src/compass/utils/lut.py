'''
Placeholder for model-based correction LUT
'''
import os
import isce3
import numpy as np
import pysolid

from compass.utils.geometry_utils import enu2los, en2az
from compass.utils.helpers import open_raster
from scipy.interpolate import RegularGridInterpolator as RGI
from osgeo import gdal
from skimage.transform import resize


def cumulative_correction_luts(burst, dem_path,
                               rg_step=200, az_step=0.25,
                               scratch_path=None):
    '''
    Sum correction LUTs and returns cumulative correction LUT in slant range
    and azimuth directions

    Parameters
    ----------
    burst: Sentinel1BurstSlc
        Sentinel-1 A/B burst SLC object
    dem_path: str
        Path to the DEM file
    rg_step: float
        LUT spacing along slant range direction
    az_step: float
        LUT spacing along azimuth direction
    scratch_path: str
        Path to the scratch directory

    Returns
    -------
    rg_lut: isce3.core.LUT2d
        Sum of slant range correction LUTs in meters as a function of azimuth
        time and slant range
    az_lut: isce3.core.LUT2d
        Sum of azimuth correction LUTs in seconds as a function of azimuth time
        and slant range
    '''
    # Get individual LUTs
    geometrical_steer_doppler, bistatic_delay, az_fm_mismatch, tides = \
        compute_geocoding_correction_luts(burst,
                                          dem_path=dem_path,
                                          rg_step=rg_step,
                                          az_step=az_step,
                                          scratch_path=scratch_path)

    # Convert to geometrical doppler from range time (seconds) to range (m)
    rg_lut_data = \
        geometrical_steer_doppler.data * isce3.core.speed_of_light * 0.5 + \
        tides[0]

    # Invert signs to correct for convention
    # TO DO: add azimuth SET to LUT
    az_lut_data = -(bistatic_delay.data + az_fm_mismatch.data)

    rg_lut = isce3.core.LUT2d(bistatic_delay.x_start,
                              bistatic_delay.y_start,
                              bistatic_delay.x_spacing,
                              bistatic_delay.y_spacing,
                              rg_lut_data)
    az_lut = isce3.core.LUT2d(bistatic_delay.x_start,
                              bistatic_delay.y_start,
                              bistatic_delay.x_spacing,
                              bistatic_delay.y_spacing,
                              az_lut_data)

    return rg_lut, az_lut


def compute_geocoding_correction_luts(burst, dem_path,
                                      rg_step=200, az_step=0.25,
                                      scratch_path=None):
    '''
    Compute slant range and azimuth LUTs corrections
    to be applied during burst geocoding
    Parameters
    ----------
    burst: Sentinel1BurstSlc
        S1-A/B burst object
    dem_path: str
        Path to the DEM required for azimuth FM rate mismatch.
    xstep: int
        LUT spacing along x/slant range in meters
    ystep: int
        LUT spacing along y/azimuth in seconds

    scratch_path: str
        Path to the scratch directory.
        If `None`, `burst.az_fm_rate_mismatch_mitigation()` will
        create temporary directory internally.

    Returns
    -------
    geometrical_steering_doppler: isce3.core.LUT2d:
        LUT2D object of total doppler (geometrical doppler +  steering doppler)
        in seconds as the function of the azimuth time and slant range.
        This correction needs to be added to the SLC tagged range time to
        get the corrected range times.

    bistatic_delay: isce3.core.LUT2d:
        LUT2D object of bistatic delay correction in seconds as a function
        of the azimuth time and slant range.
        This correction needs to be added to the SLC tagged azimuth time to
        get the corrected azimuth times.

    az_fm_mismatch: isce3.core.LUT2d:
        LUT2D object of azimuth FM rate mismatch mitigation,
        in seconds as the function of the azimuth time and slant range.
        This correction needs to be added to the SLC tagged azimuth time to
        get the corrected azimuth times.

    [rg_set, az_set]: list, np.ndarray
        List of numpy.ndarray containing SET in slant range and azimuth directions
        in meters. These corrections need to be added to the slC tagged azimuth
        and slant range times.
    '''
    # Get DEM raster
    dem_raster = isce3.io.Raster(dem_path)

    # Create directory to store SET temp results
    output_path = f'{scratch_path}/corrections'
    os.makedirs(output_path, exist_ok=True)

    # Compute Geometrical Steering Doppler
    geometrical_steering_doppler = \
        burst.doppler_induced_range_shift(range_step=rg_step, az_step=az_step)

    # Compute bistatic delay
    bistatic_delay = burst.bistatic_delay(range_step=rg_step, az_step=az_step)

    # Compute azimuth FM-rate mismatch
    az_fm_mismatch = burst.az_fm_rate_mismatch_mitigation(dem_path,
                                                          scratch_path,
                                                          range_step=rg_step,
                                                          az_step=az_step)

    # Get solid Earth tides on a very coarse grid
    # Run rdr2geo on a very coarse grid
    lon_path, lat_path, inc_path, head_path = \
        compute_rdr2geo_rasters(burst, dem_raster, output_path,
                                rg_step * 10, az_step * 10)

    # Open rdr2geo layers and feed them to SET computation
    lat = open_raster(lat_path)
    lon = open_raster(lon_path)
    inc_angle = open_raster(inc_path)
    head_angle = open_raster(head_path)

    # compute Solid Earth Tides (using pySolid)
    rg_set_temp, az_set_temp = solid_earth_tides(burst, lat, lon,
                                                 inc_angle, head_angle)

    # Resize SET to the size of the correction grid
    out_shape = bistatic_delay.data.shape
    kwargs = dict(order=1, mode='edge', anti_aliasing=True,
                  preserve_range=True)
    rg_set = resize(rg_set_temp, out_shape, **kwargs)
    az_set = resize(az_set_temp, out_shape, **kwargs)

    return geometrical_steering_doppler, bistatic_delay, az_fm_mismatch, [
        rg_set, az_set]


def solid_earth_tides(burst, lat_radar_grid, lon_radar_grid, inc_angle,
                      head_angle):
    '''
    Compute displacement due to Solid Earth Tides (SET)
    in slant range and azimuth directions

    Parameters
    ---------
    burst: Sentinel1Slc
        S1-A/B burst object
    lat_radar_grid: np.ndarray
        Latitude array on burst radargrid
    lon_radar_grid: np.ndarray
        Longitude array on burst radargrid
    inc_angle: np.ndarray
        Incident angle raster in unit of degrees
    head_angle: np.ndaaray
        Heading angle raster in unit of degrees

    Returns
    ------
    rg_set: np.ndarray
        2D array with SET displacement along LOS
    az_set: np.ndarray
        2D array with SET displacement along azimuth
    '''

    # Extract top-left coordinates from burst polygon
    lon_min, lat_min, _, _ = burst.border[0].bounds

    # Generate the atr object to run pySolid. We compute SET on a
    # 2.5 km x 2.5 km coarse grid
    margin = 0.1
    lat_start = lat_min - margin
    lon_start = lon_min - margin

    atr = {
        'LENGTH': 25,
        'WIDTH': 100,
        'X_FIRST': lon_start,
        'Y_FIRST': lat_start,
        'X_STEP': 0.023,
        'Y_STEP': 0.023
    }

    # Run pySolid and get SET in ENU coordinate system
    (set_e,
     set_n,
     set_u) = pysolid.calc_solid_earth_tides_grid(burst.sensing_start, atr,
                                                  display=False, verbose=True)

    # Resample SET from geographical grid to radar grid
    # Generate the lat/lon arrays for the SET geogrid
    lat_geo_array = np.linspace(atr['Y_FIRST'],
                                lat_start + atr['Y_STEP'] * atr['LENGTH'],
                                num=atr['LENGTH'])
    lon_geo_array = np.linspace(atr['X_FIRST'],
                                lon_start + atr['X_STEP'] * atr['WIDTH'],
                                num=atr['WIDTH'])

    # Use scipy RGI to resample SET from geocoded to radar coordinates
    pts_src = (np.flipud(lat_geo_array), lon_geo_array)
    pts_dst = (lat_radar_grid.flatten(), lon_radar_grid.flatten())

    rdr_set_e = resample_set(set_e, pts_src, pts_dst).reshape(
        lat_radar_grid.shape)
    rdr_set_n = resample_set(set_n, pts_src, pts_dst).reshape(
        lat_radar_grid.shape)
    rdr_set_u = resample_set(set_u, pts_src, pts_dst).reshape(
        lat_radar_grid.shape)

    # Convert SET from ENU to range/azimuth coordinates
    # Note: rdr2geo heading angle is measured wrt to the East and it is positive
    # anti-clockwise. To convert ENU to LOS, we need the azimuth angle which is
    # measured from the north and positive anti-clockwise
    # azimuth_angle = heading + 90
    set_rg = enu2los(rdr_set_e, rdr_set_n, rdr_set_u, inc_angle,
                     az_angle=head_angle + 90.0)
    set_az = en2az(rdr_set_e, rdr_set_n, head_angle - 90.0)

    return set_rg, set_az


def compute_rdr2geo_rasters(burst, dem_raster, output_path,
                            rg_step, az_step):
    '''
    Get latitude, longitude, incidence and
    azimuth angle on multi-looked radar grid

    Parameters
    ----------
    burst: Sentinel1Slc
        S1-A/B burst object
    dem_raster: isce3.io.Raster
        ISCE3 object including DEM raster
    output_path: str
        Path where to save output rasters
    rg_step: float
        Spacing of radar grid along slant range
    az_step: float
        Spacing of the radar grid along azimuth

    Returns
    -------
    x_path: str
        Path to longitude raster
    y_path: str
        Path to latitude raster
    inc_path: str
        Path to incidence angle raster
    head_path: str
        Path to heading angle raster
    '''

    # Some ancillary inputs
    epsg = dem_raster.get_epsg()
    proj = isce3.core.make_projection(epsg)
    ellipsoid = proj.ellipsoid

    # Get radar and doppler grid
    width_rdr_grid, length_rdr_grid = [vec.size for vec in
                                       burst._steps_to_vecs(rg_step, az_step)]

    rdr_grid = isce3.product.RadarGridParameters(
        burst.as_isce3_radargrid().sensing_start,
        burst.wavelength,
        1.0 / az_step,
        burst.starting_range,
        rg_step,
        isce3.core.LookSide.Right,
        length_rdr_grid,
        width_rdr_grid,
        burst.as_isce3_radargrid().ref_epoch
    )

    grid_doppler = isce3.core.LUT2d()

    # Initialize the rdr2geo object
    rdr2geo_obj = isce3.geometry.Rdr2Geo(rdr_grid, burst.orbit,
                                         ellipsoid, grid_doppler,
                                         threshold=1.0e8)

    # Get the rdr2geo raster needed for SET computation
    topo_output = {'x': (True, gdal.GDT_Float64),
                   'y': (True, gdal.GDT_Float64),
                   'incidence_angle': (True, gdal.GDT_Float32),
                   'heading_angle': (True, gdal.GDT_Float32)}
    raster_list = [
        isce3.io.Raster(f'{output_path}/{fname}.rdr', rdr_grid.width,
                        rdr_grid.length, 1, dtype, 'ENVI')
        if enabled else None
        for fname, (enabled, dtype) in topo_output.items()]
    x_raster, y_raster, incidence_raster, heading_raster = raster_list

    # Run rdr2geo on coarse radar grid
    rdr2geo_obj.topo(dem_raster, x_raster, y_raster,
                     incidence_angle_raster=incidence_raster,
                     heading_angle_raster=heading_raster)

    # Return file path to rdr2geo layers
    paths = [f'{output_path}/{fname}.rdr' for fname in topo_output.keys()]

    return paths[0], paths[1], paths[2], paths[3]


def resample_set(geo_tide, pts_src, pts_dest):
    '''
    Use scipy RegularGridInterpolator to resample geo_tide
    from a geographical to a radar grid

    Parameters
    ----------
    geo_tide: np.ndarray
        Tide displacement component on geographical grid
    pts_src: tuple of ndarray
        Points defining the source rectangular regular grid for resampling
    pts_dest: tuple of ndarray
        Points defining the destination grid for resampling
    Returns
    -------
    rdr_tide: np.ndarray
        Tide displacement component resampled on radar grid
    '''

    # Flip tide displacement component to be consistent with flipped latitudes
    geo_tide = np.flipud(geo_tide)
    rgi_func = RGI(pts_src, geo_tide, method='nearest',
                   bounds_error=False, fill_value=0)
    rdr_tide = rgi_func(pts_dest)
    return rdr_tide
