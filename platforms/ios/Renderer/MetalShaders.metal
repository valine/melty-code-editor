#include <metal_stdlib>
using namespace metal;

// Texture coordinates in retained Melty tiles are GL-oriented (v=1 at the
// top). Keep that public contract while Metal storage itself is top-down.
constexpr sampler nearest_gl(coord::normalized, address::clamp_to_zero, filter::nearest);
constexpr sampler linear_gl(coord::normalized, address::clamp_to_edge, filter::linear);
float4 sample_gl(texture2d<float> t, float2 uv) { return t.sample(nearest_gl, float2(uv.x, 1-uv.y)); }
float4 sample_linear_gl(texture2d<float> t, float2 uv) { return t.sample(linear_gl, float2(uv.x, 1-uv.y)); }
float4 read_gl(texture2d<float> t, int2 p, int level) {
    if (any(p < int2(0)) || any(p >= int2(t.get_width(level), t.get_height(level)))) return float4(0);
    return t.read(uint2(p.x, int(t.get_height(level))-1-p.y), level);
}
int2 size_gl(texture2d<float> t, int level) { return int2(t.get_width(level), t.get_height(level)); }

struct QuadUniforms { float4 rect; float4 size; float4 uv; };
struct QuadOut { float4 position [[position]]; float2 uv; };
vertex QuadOut quad_vertex(uint index [[vertex_id]], constant QuadUniforms &u [[buffer(0)]]) {
    const float2 v[3] = {float2(0,0), float2(2,0), float2(0,2)};
    float2 p = u.rect.xy + v[index] * u.rect.zw;
    return {float4(p/u.size.xy*2-1, 0, 1), u.uv.zw + v[index]*u.uv.xy};
}

struct ImVertex { packed_float2 position; packed_float2 uv; uint color; };
struct MeshOut { float4 position [[position]]; float2 uv; float4 color; };
struct MeshUniforms { float4 display; float4 hdr; float4 text; };
float3 srgb_linear(float3 c) { return select(c/12.92, pow((c+0.055)/1.055, float3(2.4)), c>=0.04045); }
float3 linear_srgb(float3 c) {
    c = clamp(c, 0.0, 1.0);
    return select(c*12.92, 1.055*pow(c,float3(1/2.4))-0.055, c>=0.0031308);
}
float4 decode_color(uint packed, float range, float octaves) {
    float3 code = float3(packed&255, (packed>>8)&255, (packed>>16)&255);
    float alpha = float((packed>>24)&127)/127;
    float3 color;
    if (packed & 0x80000000) color=srgb_linear(code/255);
    else {
        float3 p3=select(float3(0), range*exp2(octaves*((code-1)/254-1)), code>=0.5);
        color=float3x3(float3(1.2247453,-0.0420581,-0.0196423),
                       float3(-0.2249044,1.0420810,-0.0786549),
                       float3(0,0,1.0985372))*p3;
    }
    return float4(color*alpha,alpha);
}
vertex MeshOut mesh_vertex(uint i [[vertex_id]], const device ImVertex *v [[buffer(0)]],
                          constant MeshUniforms &u [[buffer(1)]]) {
    ImVertex a=v[i]; float2 p=float2(a.position);
    return {float4(p.x/u.display.x*2-1, 1-p.y/u.display.y*2, 0, 1),
            float2(a.uv),decode_color(a.color,u.hdr.x,u.hdr.y)};
}
fragment float4 mesh_fragment(MeshOut in [[stage_in]], constant MeshUniforms &u [[buffer(1)]],
                              texture2d<float> tex [[texture(0)]], texture2d<float> context [[texture(1)]],
                              texture2d<float> occlusion [[texture(2)]]) {
    if (u.text.w >= 0 && occlusion.read(uint2(in.position.xy)).r > u.text.w) discard_fragment();
    float4 color=float4(in.color.rgb/max(in.color.a,1e-6),in.color.a);
    // The font atlas's UVs are already top-down, while cached tiles/images
    // retain Melty's bottom-up contract. u.hdr.w selects that distinction.
    float4 t=tex.sample(linear_gl,float2(in.uv.x,u.hdr.w>0 ? in.uv.y : 1-in.uv.y));
    float alpha=t.a;
    if(u.hdr.w>0 && all(fwidth(in.uv)>0)) {
        float peak=max(max(color.r,color.g),color.b);
        if(peak>u.hdr.z) color.rgb*=u.hdr.z/peak;
        if(u.text.x>0) {
            float behind=max(0.0,dot(context.read(uint2(in.position.xy)).rgb,float3(0.2126,0.7152,0.0722)));
            float proposed=max(0.0,dot(color.rgb,float3(0.2126,0.7152,0.0722)));
            float ratio=(max(behind,proposed)+0.05)/(min(behind,proposed)+0.05);
            if(ratio<u.text.y) {
                float dark=(behind+0.05)/u.text.y-0.05, light=(behind+0.05)*u.text.y-0.05;
                float toDark=dark>=0 && proposed>0 ? clamp((proposed-dark)/proposed,0.0,1.0) : 2;
                float toLight=light<=1 && proposed<1 ? clamp((light-proposed)/(1-proposed),0.0,1.0) : 2;
                color.rgb=toDark<=toLight ? mix(color.rgb,float3(0),toDark) : mix(color.rgb,float3(1),toLight);
            }
        }
        alpha=pow(alpha,1/max(u.text.z,0.001));
    }
    return float4(color.rgb*t.rgb,color.a*alpha);
}

fragment float4 copy_fragment(QuadOut in [[stage_in]], texture2d<float> tex [[texture(0)]]) {
    return sample_gl(tex,in.uv);
}
fragment float4 present_fragment(QuadOut in [[stage_in]], texture2d<float> tex [[texture(0)]]) {
    float4 c=sample_gl(tex,in.uv); float a=clamp(c.a,0.0,1.0);
    return float4(linear_srgb(c.rgb/max(a,1e-6))*a,a);
}
fragment float4 solid_fragment(QuadOut in [[stage_in]], constant float4 *u [[buffer(1)]]) { return u[0]; }
