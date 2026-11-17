# SPDX-License-Identifier: GPL-3.0-or-later
"""TweenGlass scaling / sharpening filters (GLSL for GStreamer's glshader).

Each filter is one or two fragment-shader passes run on the GPU between the
capture and the presenter:

  pass 1 (scale): source size -> canvas size, letterboxed (aspect kept)
  pass 2 (optional, canvas size): sharpening

Ported filters and their licenses (all permissive, GPL-compatible):

* FSR 1 (EASU + RCAS) — AMD FidelityFX Super Resolution 1.0,
  Copyright (c) 2021 Advanced Micro Devices, Inc. — MIT license.
  Ported from ffx_fsr1.h (non-packed 32-bit path); gather4 replaced by
  single-texel fetches, approximate rcp/rsqrt replaced by exact math.
* SGSR 1 — Snapdragon Game Super Resolution,
  Copyright (c) 2023-2025 Qualcomm Innovation Center, Inc. — BSD-3-Clause.
  Ported from sgsr1_shader_mobile.frag (RGBA mode).
* CAS — AMD FidelityFX Contrast Adaptive Sharpening (sharpen-only path),
  Copyright (c) 2020 Advanced Micro Devices, Inc. — MIT license.
* Nearest, Integer, Bicubic (Catmull-Rom), Sharp bilinear: textbook kernels.

Full license texts: LICENSES/ in the repository.

Lossless Scaling's own "LS1" scaler is closed source and is not available.
"""

from __future__ import annotations

# Filter ids exposed to the UI, in display order.
FILTERS = ["off", "fsr", "sgsr", "bcas", "bicubic", "sharp_bilinear", "integer", "nearest"]
# Filters whose result depends on the sharpness setting.
SHARPNESS_FILTERS = {"fsr", "sgsr", "bcas"}

_HEADER = """
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform sampler2D tex;
uniform float in_w;
uniform float in_h;
uniform float out_w;
uniform float out_h;
uniform float sharpness;   // 0 .. 1
"""

# Shared helpers for the scaling pass: letterbox mapping and texel fetches.
_SCALE_COMMON = """
vec2 IN;      // input size in px
float SCALE;  // output px per input px
bool IDENT;   // 1:1 (fullscreen video on a same-size canvas): nothing to scale

vec3 T(vec2 ij) {  // texel (integer coords, clamped)
    ij = clamp(ij, vec2(0.0), IN - 1.0);
    return texture2D(tex, (ij + 0.5) / IN).SWZ;
}

vec3 bilinear(vec2 p) {  // p: continuous input px (texel centres at .5)
    vec2 q = p - 0.5;
    vec2 f = fract(q);
    vec2 i = floor(q);
    return mix(mix(T(i), T(i + vec2(1.0, 0.0)), f.x),
               mix(T(i + vec2(0.0, 1.0)), T(i + vec2(1.0, 1.0)), f.x), f.y);
}

// Maps this output pixel to input px; returns false in the letterbox bars.
bool map_to_input(out vec2 p, bool integer_scale) {
    IN = vec2(in_w, in_h);
    vec2 OUT = vec2(out_w, out_h);
    SCALE = min(OUT.x / IN.x, OUT.y / IN.y);
    if (integer_scale && SCALE >= 1.0) SCALE = floor(SCALE + 1e-3);
    vec2 size = IN * SCALE;
    vec2 off = floor((OUT - size) * 0.5);
    vec2 opx = v_texcoord * OUT;
    p = (opx - off) / SCALE;
    IDENT = abs(SCALE - 1.0) < 1e-3;
    return all(greaterThanEqual(p, vec2(0.0))) && all(lessThan(p, IN));
}
"""

_NEAREST = """
void main() {
    vec2 p;
    if (!map_to_input(p, INTEGER)) { gl_FragColor = OUT(0.0, 0.0, 0.0, 1.0); return; }
    gl_FragColor = OUT(T(floor(p)), 1.0);
}
"""

_SHARP_BILINEAR = """
void main() {
    vec2 p;
    if (!map_to_input(p, false)) { gl_FragColor = OUT(0.0, 0.0, 0.0, 1.0); return; }
    // Pixel-art friendly: nearest inside a texel, bilinear only across the
    // (one output pixel wide) seam between texels.
    vec2 q = p - 0.5;
    vec2 i = floor(q);
    vec2 f = clamp((fract(q) - 0.5) * max(SCALE, 1.0) + 0.5, 0.0, 1.0);
    vec3 c = mix(mix(T(i), T(i + vec2(1.0, 0.0)), f.x),
                 mix(T(i + vec2(0.0, 1.0)), T(i + vec2(1.0, 1.0)), f.x), f.y);
    gl_FragColor = OUT(c, 1.0);
}
"""

_BICUBIC = """
vec4 cr(float t) {  // Catmull-Rom weights for taps -1, 0, 1, 2
    float t2 = t * t, t3 = t2 * t;
    return vec4(-0.5 * t3 + t2 - 0.5 * t,
                 1.5 * t3 - 2.5 * t2 + 1.0,
                -1.5 * t3 + 2.0 * t2 + 0.5 * t,
                 0.5 * t3 - 0.5 * t2);
}
void main() {
    vec2 p;
    if (!map_to_input(p, false)) { gl_FragColor = OUT(0.0, 0.0, 0.0, 1.0); return; }
    if (IDENT) { gl_FragColor = OUT(T(floor(p)), 1.0); return; }
    vec2 q = p - 0.5;
    vec2 i = floor(q);
    vec2 f = q - i;
    vec4 wx = cr(f.x), wy = cr(f.y);
    vec3 c = vec3(0.0);
    for (int y = 0; y < 4; y++) {
        vec3 row = T(i + vec2(-1.0, float(y) - 1.0)) * wx.x + T(i + vec2(0.0, float(y) - 1.0)) * wx.y
                 + T(i + vec2(1.0, float(y) - 1.0)) * wx.z + T(i + vec2(2.0, float(y) - 1.0)) * wx.w;
        c += row * (y == 0 ? wy.x : y == 1 ? wy.y : y == 2 ? wy.z : wy.w);
    }
    gl_FragColor = OUT(clamp(c, 0.0, 1.0), 1.0);
}
"""

# --- AMD FidelityFX FSR 1 EASU (MIT) ---------------------------------------
_EASU = """
float rcp(float x) { return 1.0 / max(x, 1.0 / 32768.0); }
float L(vec3 c) { return c.b * 0.5 + (c.r * 0.5 + c.g); }  // luma times 2

void easu_set(inout vec2 dir, inout float len, vec2 pp, float w,
              float lA, float lB, float lC, float lD, float lE) {
    float dc = lD - lC, cb = lC - lB;
    float lenX = rcp(max(abs(dc), abs(cb)));
    float dirX = lD - lB;
    dir.x += dirX * w;
    lenX = clamp(abs(dirX) * lenX, 0.0, 1.0);
    lenX *= lenX;
    len += lenX * w;
    float ec = lE - lC, ca = lC - lA;
    float lenY = rcp(max(abs(ec), abs(ca)));
    float dirY = lE - lA;
    dir.y += dirY * w;
    lenY = clamp(abs(dirY) * lenY, 0.0, 1.0);
    lenY *= lenY;
    len += lenY * w;
}

void easu_tap(inout vec3 aC, inout float aW, vec2 off, vec2 dir, vec2 len, float lob, float clp, vec3 c) {
    vec2 v;
    v.x = off.x * dir.x + off.y * dir.y;
    v.y = off.x * (-dir.y) + off.y * dir.x;
    v *= len;
    float d2 = min(v.x * v.x + v.y * v.y, clp);
    float wB = 2.0 / 5.0 * d2 - 1.0;
    float wA = lob * d2 - 1.0;
    wB *= wB;
    wA *= wA;
    wB = 25.0 / 16.0 * wB - (25.0 / 16.0 - 1.0);
    float w = wB * wA;
    aC += c * w;
    aW += w;
}

void main() {
    vec2 p;
    if (!map_to_input(p, false)) { gl_FragColor = OUT(0.0, 0.0, 0.0, 1.0); return; }
    if (IDENT) { gl_FragColor = OUT(T(floor(p)), 1.0); return; }
    vec2 pp = p - 0.5;
    vec2 fp = floor(pp);
    pp -= fp;
    //    b c
    //  e f g h
    //  i j k l
    //    n o
    vec3 b = T(fp + vec2(0.0, -1.0)), c = T(fp + vec2(1.0, -1.0));
    vec3 e = T(fp + vec2(-1.0, 0.0)), f = T(fp), g = T(fp + vec2(1.0, 0.0)), h = T(fp + vec2(2.0, 0.0));
    vec3 i = T(fp + vec2(-1.0, 1.0)), j = T(fp + vec2(0.0, 1.0)), k = T(fp + vec2(1.0, 1.0)), l = T(fp + vec2(2.0, 1.0));
    vec3 n = T(fp + vec2(0.0, 2.0)), o = T(fp + vec2(1.0, 2.0));
    float bL = L(b), cL = L(c), eL = L(e), fL = L(f), gL = L(g), hL = L(h);
    float iL = L(i), jL = L(j), kL = L(k), lL = L(l), nL = L(n), oL = L(o);
    vec2 dir = vec2(0.0);
    float len = 0.0;
    easu_set(dir, len, pp, (1.0 - pp.x) * (1.0 - pp.y), bL, eL, fL, gL, jL);
    easu_set(dir, len, pp, pp.x * (1.0 - pp.y), cL, fL, gL, hL, kL);
    easu_set(dir, len, pp, (1.0 - pp.x) * pp.y, fL, iL, jL, kL, nL);
    easu_set(dir, len, pp, pp.x * pp.y, gL, jL, kL, lL, oL);
    vec2 dir2 = dir * dir;
    float dirR = dir2.x + dir2.y;
    bool zro = dirR < 1.0 / 32768.0;
    dirR = zro ? 1.0 : inversesqrt(dirR);
    dir.x = zro ? 1.0 : dir.x;
    dir *= dirR;
    len = len * 0.5;
    len *= len;
    float stretch = (dir.x * dir.x + dir.y * dir.y) * rcp(max(abs(dir.x), abs(dir.y)));
    vec2 len2 = vec2(1.0 + (stretch - 1.0) * len, 1.0 - 0.5 * len);
    float lob = 0.5 + ((1.0 / 4.0 - 0.04) - 0.5) * len;
    float clp = rcp(lob);
    vec3 min4 = min(min(f, g), min(j, k));
    vec3 max4 = max(max(f, g), max(j, k));
    vec3 aC = vec3(0.0);
    float aW = 0.0;
    easu_tap(aC, aW, vec2(0.0, -1.0) - pp, dir, len2, lob, clp, b);
    easu_tap(aC, aW, vec2(1.0, -1.0) - pp, dir, len2, lob, clp, c);
    easu_tap(aC, aW, vec2(-1.0, 1.0) - pp, dir, len2, lob, clp, i);
    easu_tap(aC, aW, vec2(0.0, 1.0) - pp, dir, len2, lob, clp, j);
    easu_tap(aC, aW, vec2(0.0, 0.0) - pp, dir, len2, lob, clp, f);
    easu_tap(aC, aW, vec2(-1.0, 0.0) - pp, dir, len2, lob, clp, e);
    easu_tap(aC, aW, vec2(1.0, 1.0) - pp, dir, len2, lob, clp, k);
    easu_tap(aC, aW, vec2(2.0, 1.0) - pp, dir, len2, lob, clp, l);
    easu_tap(aC, aW, vec2(2.0, 0.0) - pp, dir, len2, lob, clp, h);
    easu_tap(aC, aW, vec2(1.0, 0.0) - pp, dir, len2, lob, clp, g);
    easu_tap(aC, aW, vec2(1.0, 2.0) - pp, dir, len2, lob, clp, o);
    easu_tap(aC, aW, vec2(0.0, 2.0) - pp, dir, len2, lob, clp, n);
    gl_FragColor = OUT(min(max4, max(min4, aC / aW)), 1.0);
}
"""

# --- Qualcomm Snapdragon GSR 1 (BSD-3-Clause), RGBA mode --------------------
_SGSR = """
float fastLanczos2(float x) {
    float wA = x - 4.0;
    float wB = x * wA - wA;
    wA *= wA;
    return wB * wA;
}
vec2 weightY(float dx, float dy, float c, float std) {
    float x = ((dx * dx) + (dy * dy)) * 0.55 + clamp(abs(c) * std, 0.0, 1.0);
    float w = fastLanczos2(x);
    return vec2(w, w * c);
}
// textureGather(.., 1) emulation: green channel of the 2x2 texels around corner px (i, j).
vec4 gatherG(vec2 corner) {
    return vec4(T(corner + vec2(-1.0, 0.0)).g, T(corner).g, T(corner + vec2(0.0, -1.0)).g,
                T(corner + vec2(-1.0, -1.0)).g);
}
void main() {
    vec2 p;
    if (!map_to_input(p, false)) { gl_FragColor = OUT(0.0, 0.0, 0.0, 1.0); return; }
    float edgeThreshold = 8.0 / 255.0;
    float edgeSharpness = mix(1.5, 2.5, sharpness);
    vec4 color = vec4(bilinear(p), 0.0);
    vec2 imgCoord = p - 0.5 + vec2(-0.5, 0.5);
    vec2 imgCoordPixel = floor(imgCoord);
    vec2 pl = imgCoord - imgCoordPixel;
    vec2 corner = imgCoordPixel;          // gather position, in px
    vec4 left = gatherG(corner);
    float edgeVote = abs(left.z - left.y) + abs(color.g - left.y) + abs(color.g - left.z);
    if (edgeVote > edgeThreshold) {
        corner.x += 1.0;
        vec4 right = gatherG(corner + vec2(1.0, 0.0));
        vec4 upDown;
        upDown.xy = gatherG(corner + vec2(0.0, -1.0)).wz;
        upDown.zw = gatherG(corner + vec2(0.0, 1.0)).yx;
        float mean = (left.y + left.z + right.x + right.w) * 0.25;
        left -= vec4(mean);
        right -= vec4(mean);
        upDown -= vec4(mean);
        color.w = color.g - mean;
        float sum = abs(left.x) + abs(left.y) + abs(left.z) + abs(left.w)
                  + abs(right.x) + abs(right.y) + abs(right.z) + abs(right.w)
                  + abs(upDown.x) + abs(upDown.y) + abs(upDown.z) + abs(upDown.w);
        float std = 2.181818 / max(sum, 1e-5);
        vec2 aWY = weightY(pl.x, pl.y + 1.0, upDown.x, std);
        aWY += weightY(pl.x - 1.0, pl.y + 1.0, upDown.y, std);
        aWY += weightY(pl.x - 1.0, pl.y - 2.0, upDown.z, std);
        aWY += weightY(pl.x, pl.y - 2.0, upDown.w, std);
        aWY += weightY(pl.x + 1.0, pl.y - 1.0, left.x, std);
        aWY += weightY(pl.x, pl.y - 1.0, left.y, std);
        aWY += weightY(pl.x, pl.y, left.z, std);
        aWY += weightY(pl.x + 1.0, pl.y, left.w, std);
        aWY += weightY(pl.x - 1.0, pl.y - 1.0, right.x, std);
        aWY += weightY(pl.x - 2.0, pl.y - 1.0, right.y, std);
        aWY += weightY(pl.x - 2.0, pl.y, right.z, std);
        aWY += weightY(pl.x - 1.0, pl.y, right.w, std);
        float finalY = aWY.y / aWY.x;
        float maxY = max(max(left.y, left.z), max(right.x, right.w));
        float minY = min(min(left.y, left.z), min(right.x, right.w));
        finalY = clamp(edgeSharpness * finalY, minY, maxY);
        float deltaY = clamp(finalY - color.w, -23.0 / 255.0, 23.0 / 255.0);
        color.rgb = clamp(color.rgb + deltaY, 0.0, 1.0);
    }
    gl_FragColor = OUT(color.rgb, 1.0);
}
"""

# Second pass, at canvas size: neighbours are one output pixel away.
_POST_COMMON = """
vec3 P(vec2 d) { return texture2D(tex, v_texcoord + d / vec2(out_w, out_h)).rgb; }
"""

# --- AMD FidelityFX FSR 1 RCAS (MIT) ----------------------------------------
_RCAS = """
#define FSR_RCAS_LIMIT (0.25 - (1.0 / 16.0))
void main() {
    vec3 b = P(vec2(0.0, -1.0)), d = P(vec2(-1.0, 0.0)), e = P(vec2(0.0)), f = P(vec2(1.0, 0.0)), h = P(vec2(0.0, 1.0));
    // sharpness 0..1 -> RCAS "stops" 2..0 (0 = maximum sharpening)
    float con = exp2(-(1.0 - sharpness) * 2.0);
    vec3 mn4 = min(min(b, d), min(f, h));
    vec3 mx4 = max(max(b, d), max(f, h));
    vec2 peakC = vec2(1.0, -4.0);
    vec3 hitMin = min(mn4, e) / (4.0 * mx4 + 1e-5);
    vec3 hitMax = (peakC.x - max(mx4, e)) / (4.0 * mn4 + peakC.y);
    vec3 lobeRGB = max(-hitMin, hitMax);
    float lobe = max(-FSR_RCAS_LIMIT, min(max(lobeRGB.r, max(lobeRGB.g, lobeRGB.b)), 0.0)) * con;
    vec3 c = (lobe * (b + d + h + f) + e) / (4.0 * lobe + 1.0);
    gl_FragColor = OUT(clamp(c, 0.0, 1.0), 1.0);
}
"""

# --- AMD FidelityFX CAS, sharpen-only (MIT) ---------------------------------
_CAS = """
void main() {
    vec3 a = P(vec2(-1.0, -1.0)), b = P(vec2(0.0, -1.0)), c = P(vec2(1.0, -1.0));
    vec3 d = P(vec2(-1.0, 0.0)), e = P(vec2(0.0)), f = P(vec2(1.0, 0.0));
    vec3 g = P(vec2(-1.0, 1.0)), h = P(vec2(0.0, 1.0)), i = P(vec2(1.0, 1.0));
    vec3 mn = min(min(min(d, e), min(f, b)), h);
    vec3 mn2 = min(mn, min(min(a, c), min(g, i)));
    mn += mn2;
    vec3 mx = max(max(max(d, e), max(f, b)), h);
    vec3 mx2 = max(mx, max(max(a, c), max(g, i)));
    mx += mx2;
    vec3 amp = sqrt(clamp(min(mn, 2.0 - mx) / max(mx, 1e-5), 0.0, 1.0));
    float peak = -1.0 / mix(8.0, 5.0, sharpness);
    vec3 w = amp * peak;
    vec3 o = (b * w + d * w + f * w + h * w + e) / (1.0 + 4.0 * w);
    gl_FragColor = OUT(clamp(o, 0.0, 1.0), 1.0);
}
"""


# Channel order: the presenter wants BGRA bytes. Instead of a final
# glcolorconvert pass, the last shader writes swizzled, so the downloaded
# buffer (caps say RGBA) already holds BGRA bytes. Downstream only burns the
# grey HUD (symmetric in R/B) and hands raw bytes to the presenter.
_IN_RGB = "#define SWZ rgb\n"
_OUT_BGR = "vec4 OUT(vec3 c, float a) { return vec4(c.bgr, a); }\nvec4 OUT(float r, float g, float b, float a) { return vec4(b, g, r, a); }\n"
_OUT_RGB = "vec4 OUT(vec3 c, float a) { return vec4(c, a); }\nvec4 OUT(float r, float g, float b, float a) { return vec4(r, g, b, a); }\n"


def passes(filter_id: str) -> list[str]:
    """Fragment shaders for a filter: [scale] or [scale, sharpen]."""
    shaders = _passes(filter_id)
    out = []
    for n, src in enumerate(shaders):
        first, last = n == 0, n == len(shaders) - 1
        out.append(_HEADER + _IN_RGB + (_OUT_BGR if last else _OUT_RGB) + src)
    return out


def _passes(filter_id: str) -> list[str]:
    scale = _SCALE_COMMON
    post = _POST_COMMON
    if filter_id == "nearest":
        return [scale + "#define INTEGER false\n" + _NEAREST]
    if filter_id == "integer":
        return [scale + "#define INTEGER true\n" + _NEAREST]
    if filter_id == "sharp_bilinear":
        return [scale + _SHARP_BILINEAR]
    if filter_id == "bicubic":
        return [scale + _BICUBIC]
    if filter_id == "bcas":
        return [scale + _BICUBIC, post + _CAS]
    if filter_id == "fsr":
        return [scale + _EASU, post + _RCAS]
    if filter_id == "sgsr":
        return [scale + _SGSR]
    raise ValueError(f"unknown filter {filter_id!r}")


def gl_chain(filter_id: str, canvas: tuple[int, int]) -> str:
    """gst-launch fragment: raw video in -> filtered canvas-sized frames in system
    memory, BGRA byte order (caps say RGBA)."""
    w, h = canvas
    shaders = passes(filter_id)
    chain = ("glupload ! glcolorconvert ! video/x-raw(memory:GLMemory),format=RGBA ! "
             f"glshader name=tgf0 ! video/x-raw(memory:GLMemory),width={w},height={h},pixel-aspect-ratio=1/1 ! ")
    for n in range(1, len(shaders)):
        chain += f"glshader name=tgf{n} ! "
    # NOTE: labelled RGBA, bytes are BGRA (see _OUT_BGR).
    chain += "gldownload ! video/x-raw,format=RGBA"
    return chain


def configure(pipe, Gst, filter_id: str, canvas: tuple[int, int], sharpness: float) -> None:
    """Load the shaders and keep the uniforms in sync with the source size."""
    shaders = passes(filter_id)
    elems = [pipe.get_by_name(f"tgf{n}") for n in range(len(shaders))]
    state = {"in": (canvas[0], canvas[1])}

    def apply():
        iw, ih = state["in"]
        u = Gst.Structure.new_from_string(
            f"uniforms, in_w=(float){iw}, in_h=(float){ih}, out_w=(float){canvas[0]}, "
            f"out_h=(float){canvas[1]}, sharpness=(float){max(0.0, min(1.0, sharpness))}")
        for e in elems:
            e.set_property("uniforms", u)

    for e, src in zip(elems, shaders):
        e.set_property("fragment", src)
    apply()

    def on_caps(_pad, info):
        ev = info.get_event()
        if ev.type == Gst.EventType.CAPS:
            st = ev.parse_caps().get_structure(0)
            size = (st.get_value("width"), st.get_value("height"))
            if size != state["in"]:
                state["in"] = size
                apply()
        return Gst.PadProbeReturn.OK

    elems[0].get_static_pad("sink").add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, on_caps)
