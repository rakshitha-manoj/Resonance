/* Python bridge for the autoencoder path (see models/training/ae_bridge.py): the node's own C, compiled as a
   shared library. gcc -O2 -ffp-contract=off -shared -I../include ae_lib.c */
#include "ae.h"

#ifdef _WIN32
#define AE_API __declspec(dllexport)
#else
#define AE_API
#endif

/* n windows of 128 int16 (az, gx contiguous) -> n x 20 base features */
AE_API void ae_base_batch(int n, const int16_t *az, const int16_t *gx, float *out) {
    for (int w = 0; w < n; w++) ae_base(az + (long)w * WIN_N, gx + (long)w * WIN_N, out + (long)w * AE_NBASE);
}

AE_API int ae_lib_n_in(void) { return AE_N_IN; }
AE_API int ae_lib_comm_n(void) { return AE_COMM_N; }

/* One node: commission on comm (AE_COMM_N x 20 base windows), then n windows of base features ->
   ratio[n] (score / median commissioning score), z_out (n x AE_N_IN, may be NULL) = normalised inputs, *ref_out = reference score. */
AE_API void ae_eval_node(const float *comm, int n, const float *base, float *ratio, float *z_out, float *ref_out) {
    AeNode nd;
    ae_node_reset(&nd);
    for (int w = 0; w < AE_COMM_N; w++) ae_node_commission(&nd, comm + (long)w * AE_NBASE);
    for (int w = 0; w < n; w++) {
        ratio[w] = ae_node_ratio(&nd, base + (long)w * AE_NBASE, NULL);
        if (z_out) {
            float x[AE_N_IN];
            ae_make_input(base + (long)w * AE_NBASE, x);
            ae_normalise(&nd, x, z_out + (long)w * AE_N_IN);
        }
    }
    *ref_out = nd.ref;
}

AE_API void ae_quant_batch(int n, const float *x, int8_t *q) {
    for (long i = 0; i < (long)n * AE_N_IN; i++) q[i] = ae_quant_in(x[i]);
}

AE_API void ae_run_batch(int n, const int8_t *in, int8_t *out) {
    for (int i = 0; i < n; i++) ae_run_int8(in + (long)i * AE_N_IN, out + (long)i * AE_N_IN);
}
