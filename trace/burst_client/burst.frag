#version 450
// Per-pixel dependent ALU loop: GPU cost = work-area pixels x iters, independent of the
// window size (the viewport is clamped to the work area).
layout(push_constant) uniform PC { uint iters; uint seed; } pc;
layout(location = 0) out vec4 color;

void main() {
    float a = fract(gl_FragCoord.x * 0.0137 + float(pc.seed) * 0.618);
    float b = fract(gl_FragCoord.y * 0.0291);
    for (uint i = 0u; i < pc.iters; i++) {
        a = fract(a * 3.7 + b);
        b = fract(b * 1.3 + a * 0.7 + 0.1);
    }
    color = vec4(a, b, 0.3, 1.0);
}
