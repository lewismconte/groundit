"""Terrain tiles: PNG decoding, terrarium elevation, and mosaic sampling.

The elevation source is the AWS "Terrain Tiles" open dataset, which serves
global terrain as ordinary 256 px PNG tiles in the terrarium encoding:

    elevation_metres = (R * 256 + G + B / 256) - 32768

No API key, no account, no GDAL. That is the whole reason this tool can be a
single button rather than a setup guide.

PNG decoding has two paths. Inside Revit we hand the bytes to WPF's
PngBitmapDecoder, which is native, always loaded (pyRevit runs on WPF) and
fast. Headless we fall back to a small pure-Python decoder so the maths in
this module can be tested outside Revit. Note that System.Drawing is
deliberately NOT used: on .NET 8 (Revit 2025+) it is an out-of-band package
and loading it from IronPython is a gamble we do not need to take.

Pure Python except for the optional .NET fast path, which is probed lazily.
"""

import math

from . import geodesy

TILE_PX = 256

TERRARIUM_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/%d/%d/%d.png"

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


# ------------------------------------------------------------- PNG decoding

def _u32(buf, i):
    """Big-endian uint32 out of a bytearray, without struct.

    struct.unpack refuses bytearray on Python 2.7, and doing it by hand keeps
    one code path for both runtimes.
    """
    return (buf[i] << 24) | (buf[i + 1] << 16) | (buf[i + 2] << 8) | buf[i + 3]


def _inflate(data):
    """zlib-inflate, via zlib if present, else .NET DeflateStream."""
    try:
        import zlib
        return bytearray(zlib.decompress(bytes(data)))
    except ImportError:
        pass
    # .NET fallback. DeflateStream handles raw deflate, so skip the 2-byte
    # zlib header and let the 4-byte adler32 trailer fall off the end.
    from System.IO import MemoryStream
    from System.IO.Compression import DeflateStream, CompressionMode
    from System import Array, Byte
    payload = bytes(data[2:])
    src = MemoryStream(Array[Byte](bytearray(payload)))
    ds = DeflateStream(src, CompressionMode.Decompress)
    out = MemoryStream()
    ds.CopyTo(out)
    return bytearray(out.ToArray())


def _unfilter(raw, width, height, channels):
    """Reverse the per-scanline PNG filters. -> bytearray of raw samples."""
    stride = width * channels
    bpp = channels                      # 8-bit samples, so bytes per pixel
    out = bytearray(height * stride)
    prev = bytearray(stride)
    pos = 0
    o = 0
    for _row in range(height):
        ft = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if ft == 0:
            pass
        elif ft == 1:                   # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ft == 2:                   # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:                   # Average
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ft == 4:                   # Paeth
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                if pa <= pb and pa <= pc:
                    pr = a
                elif pb <= pc:
                    pr = b
                else:
                    pr = c
                line[i] = (line[i] + pr) & 0xFF
        else:
            raise ValueError("Unsupported PNG filter type %r" % ft)
        out[o:o + stride] = line
        o += stride
        prev = line
    return out


def decode_png_pure(data):
    """Decode an 8-bit non-interlaced PNG. -> (width, height, rgb bytearray).

    Supports colour types 2 (RGB) and 6 (RGBA); terrain tiles are type 2.
    Alpha is dropped. Palette and 16-bit are not supported and never appear
    in the tile sets we read.
    """
    buf = bytearray(data)
    if bytes(buf[:8]) != _PNG_SIG:
        raise ValueError("Not a PNG (bad signature)")

    width = height = 0
    channels = 0
    idat = bytearray()
    i = 8
    n = len(buf)
    while i + 8 <= n:
        length = _u32(buf, i)
        ctype = bytes(buf[i + 4:i + 8])
        body = i + 8
        if ctype == b"IHDR":
            width = _u32(buf, body)
            height = _u32(buf, body + 4)
            bit_depth = buf[body + 8]
            colour_type = buf[body + 9]
            interlace = buf[body + 12]
            if bit_depth != 8:
                raise ValueError("Only 8-bit PNGs are supported (got %d)" % bit_depth)
            if interlace:
                raise ValueError("Interlaced PNGs are not supported")
            if colour_type == 2:
                channels = 3
            elif colour_type == 6:
                channels = 4
            else:
                raise ValueError("Unsupported PNG colour type %d" % colour_type)
        elif ctype == b"IDAT":
            idat.extend(buf[body:body + length])
        elif ctype == b"IEND":
            break
        i = body + length + 4           # + CRC

    if not width or not channels:
        raise ValueError("PNG had no usable IHDR")

    samples = _unfilter(_inflate(idat), width, height, channels)
    if channels == 3:
        return width, height, samples
    rgb = bytearray(width * height * 3)
    for p in range(width * height):
        s, d = p * 4, p * 3
        rgb[d] = samples[s]
        rgb[d + 1] = samples[s + 1]
        rgb[d + 2] = samples[s + 2]
    return width, height, rgb


def decode_png_dotnet(data):
    """Decode a PNG with WPF. -> (width, height, rgb bytearray). None if WPF absent."""
    try:
        from System.IO import MemoryStream
        from System.Windows.Media.Imaging import (
            PngBitmapDecoder, BitmapCreateOptions, BitmapCacheOption)
        from System.Windows.Media import PixelFormats
        from System.Windows.Media.Imaging import FormatConvertedBitmap
        from System import Array, Byte
    except ImportError:
        return None

    stream = MemoryStream(Array[Byte](bytearray(data)))
    decoder = PngBitmapDecoder(stream, BitmapCreateOptions.PreservePixelFormat,
                               BitmapCacheOption.OnLoad)
    frame = decoder.Frames[0]
    # Normalise to a known layout rather than trusting the file's own format.
    converted = FormatConvertedBitmap()
    converted.BeginInit()
    converted.Source = frame
    converted.DestinationFormat = PixelFormats.Bgr24
    converted.EndInit()

    width = int(converted.PixelWidth)
    height = int(converted.PixelHeight)
    stride = width * 3
    pixels = Array.CreateInstance(Byte, stride * height)
    converted.CopyPixels(pixels, stride, 0)

    bgr = bytearray(pixels)
    rgb = bytearray(len(bgr))
    for p in range(width * height):
        j = p * 3
        rgb[j] = bgr[j + 2]
        rgb[j + 1] = bgr[j + 1]
        rgb[j + 2] = bgr[j]
    return width, height, rgb


_use_dotnet = None


def decode_png(data):
    """Decode a PNG to (width, height, rgb bytearray), fastest path available."""
    global _use_dotnet
    if _use_dotnet is None:
        probe = None
        try:
            probe = decode_png_dotnet(data)
        except Exception:
            probe = None
        _use_dotnet = probe is not None
        if probe is not None:
            return probe
    if _use_dotnet:
        return decode_png_dotnet(data)
    return decode_png_pure(data)


# ---------------------------------------------------------------- terrarium

def elevation_from_rgb(r, g, b):
    """Terrarium encoding -> metres above sea level."""
    return (r * 256.0 + g + b / 256.0) - 32768.0


class TerrainMosaic(object):
    """Decoded terrain tiles at one zoom, sampled by lon/lat in metres.

    Samples bilinearly, so the surface handed to Revit is smooth rather than
    stepped when the output grid does not line up with the tile grid (which
    it never does).
    """

    def __init__(self, zoom):
        self.zoom = int(zoom)
        self.tiles = {}                 # (x, y) -> rgb bytearray
        self._gx0 = self._gy0 = None    # global pixel bounds of what we hold
        self._gx1 = self._gy1 = None

    def add_tile(self, x, y, png_bytes):
        w, h, rgb = decode_png(png_bytes)
        if w != TILE_PX or h != TILE_PX:
            raise ValueError("Expected %dpx tiles, got %dx%d" % (TILE_PX, w, h))
        self.tiles[(int(x), int(y))] = rgb
        gx0, gy0 = int(x) * TILE_PX, int(y) * TILE_PX
        gx1, gy1 = gx0 + TILE_PX - 1, gy0 + TILE_PX - 1
        self._gx0 = gx0 if self._gx0 is None else min(self._gx0, gx0)
        self._gy0 = gy0 if self._gy0 is None else min(self._gy0, gy0)
        self._gx1 = gx1 if self._gx1 is None else max(self._gx1, gx1)
        self._gy1 = gy1 if self._gy1 is None else max(self._gy1, gy1)

    def __len__(self):
        return len(self.tiles)

    def _pixel_elev(self, gx, gy):
        """Elevation at an integer global pixel, clamped to loaded coverage."""
        gx = max(self._gx0, min(self._gx1, gx))
        gy = max(self._gy0, min(self._gy1, gy))
        tile = self.tiles.get((gx // TILE_PX, gy // TILE_PX))
        if tile is None:
            return 0.0                  # hole in a non-rectangular fetch
        i = ((gy % TILE_PX) * TILE_PX + (gx % TILE_PX)) * 3
        return elevation_from_rgb(tile[i], tile[i + 1], tile[i + 2])

    def sample(self, lon, lat):
        """Bilinear elevation in metres at a lon/lat."""
        if not self.tiles:
            return 0.0
        tx, ty = geodesy.lonlat_to_tile_xy(lon, lat, self.zoom)
        # Global pixel space, offset to pixel centres for the interpolation.
        gx = tx * TILE_PX - 0.5
        gy = ty * TILE_PX - 0.5
        x0, y0 = int(math.floor(gx)), int(math.floor(gy))
        fx, fy = gx - x0, gy - y0
        e00 = self._pixel_elev(x0, y0)
        e10 = self._pixel_elev(x0 + 1, y0)
        e01 = self._pixel_elev(x0, y0 + 1)
        e11 = self._pixel_elev(x0 + 1, y0 + 1)
        top = e00 + (e10 - e00) * fx
        bottom = e01 + (e11 - e01) * fx
        return top + (bottom - top) * fy


# --------------------------------------------------------------- output grid

def grid_dims_for_budget(bbox, max_points):
    """Grid columns/rows that fit a point budget while keeping bbox aspect.

    -> (cols, rows), each at least 2.
    """
    w, h = geodesy.bbox_size_m(bbox)
    if h <= 0 or w <= 0:
        return 2, 2
    aspect = w / h
    rows = int(math.sqrt(max(1.0, float(max_points)) / aspect))
    cols = int(rows * aspect)
    return max(2, cols), max(2, rows)


class TerrainGrid(object):
    """A regular grid of terrain samples in local metres.

    Holds the points for the toposolid and doubles as a sampler so roads and
    building bases can be draped on the same surface Revit will show.
    """

    def __init__(self, frame, bbox, cols, rows):
        self.frame = frame
        self.bbox = bbox
        self.cols = int(cols)
        self.rows = int(rows)
        west, south, east, north = bbox
        self.e0, self.n0 = frame.to_local(west, south)
        self.e1, self.n1 = frame.to_local(east, north)
        self.de = (self.e1 - self.e0) / (self.cols - 1)
        self.dn = (self.n1 - self.n0) / (self.rows - 1)
        self.z = []                     # rows of elevation, south to north
        self.min_elev = 0.0
        self.max_elev = 0.0

    def fill(self, mosaic):
        """Sample a mosaic onto the grid."""
        self.z = []
        lo, hi = None, None
        for r in range(self.rows):
            north_m = self.n0 + self.dn * r
            row = []
            for c in range(self.cols):
                east_m = self.e0 + self.de * c
                lon, lat = self.frame.to_lonlat(east_m, north_m)
                e = mosaic.sample(lon, lat)
                row.append(e)
                lo = e if lo is None else min(lo, e)
                hi = e if hi is None else max(hi, e)
            self.z.append(row)
        self.min_elev = 0.0 if lo is None else lo
        self.max_elev = 0.0 if hi is None else hi
        return self

    def sample(self, east_m, north_m):
        """Bilinear elevation at an arbitrary local point, clamped to the grid."""
        if not self.z:
            return 0.0
        fc = (east_m - self.e0) / self.de if self.de else 0.0
        fr = (north_m - self.n0) / self.dn if self.dn else 0.0
        fc = max(0.0, min(self.cols - 1.0001, fc))
        fr = max(0.0, min(self.rows - 1.0001, fr))
        c0, r0 = int(fc), int(fr)
        tx, ty = fc - c0, fr - r0
        z00 = self.z[r0][c0]
        z10 = self.z[r0][c0 + 1]
        z01 = self.z[r0 + 1][c0]
        z11 = self.z[r0 + 1][c0 + 1]
        top = z00 + (z10 - z00) * tx
        bottom = z01 + (z11 - z01) * tx
        return top + (bottom - top) * ty

    def points(self, datum=0.0):
        """Flat list of (east_m, north_m, elev_m) with datum subtracted."""
        out = []
        for r in range(self.rows):
            north_m = self.n0 + self.dn * r
            zrow = self.z[r]
            for c in range(self.cols):
                out.append((self.e0 + self.de * c, north_m, zrow[c] - datum))
        return out

    def point_count(self):
        return self.cols * self.rows
