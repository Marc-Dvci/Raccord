// Monotonic alignment kernels for the Raccord caption probe.
//
// The same two phases as wavefront.py and triton_kernels.py, written directly
// in CUDA for hosts that build the extension rather than installing Triton:
//
//   substitution_kernel  - the n x m token cost matrix, one thread per cell
//   wavefront_kernel     - one launch per anti-diagonal k = i + j; every cell on
//                          the diagonal depends only on k-1 and k-2, so the
//                          whole diagonal is independent
//   wavefront_band_kernel - the same recurrence restricted to a Sakoe-Chiba band
//                          around the diagonal, which is what a live probe
//                          actually needs: caption drift beyond the band is a
//                          different failure class, not a harder alignment
//
// Tie-breaking is strict less-than in the order diagonal, up, left. That is the
// reference implementation's order, and keeping it is what makes the traceback
// bit-identical rather than merely equally optimal. tests/test_kernels_parity.py
// asserts that property against the NumPy reference.
//
// Build:
//   nvcc -O3 -std=c++17 --compiler-options -fPIC -shared \
//        -o libraccord_align.so align_wavefront.cu
//
// This file is not required to run Raccord. With neither the extension nor
// Triton present the probe fleet uses the NumPy wavefront implementation, which
// is what the published performance numbers were measured with.

#include <cuda_runtime.h>
#include <float.h>
#include <stdint.h>

namespace {

constexpr float kSubCost = 1.0f;
constexpr float kNearCost = 0.35f;
constexpr float kGapCost = 0.6f;

}  // namespace

extern "C" {

// --- phase 1: token cost matrix ------------------------------------------
//
// Tokens arrive integer-coded from the host: identity id, 4-character prefix
// id, and a length>=4 flag. No string handling on device.
__global__ void substitution_kernel(const int32_t* __restrict__ ref_id,
                                    const int32_t* __restrict__ hyp_id,
                                    const int32_t* __restrict__ ref_pre,
                                    const int32_t* __restrict__ hyp_pre,
                                    const int8_t* __restrict__ ref_long,
                                    const int8_t* __restrict__ hyp_long,
                                    float* __restrict__ out,
                                    int n, int m) {
  const int i = blockIdx.y * blockDim.y + threadIdx.y;
  const int j = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= n || j >= m) return;

  float cost = kSubCost;
  if (ref_pre[i] == hyp_pre[j] && ref_long[i] && hyp_long[j]) cost = kNearCost;
  if (ref_id[i] == hyp_id[j]) cost = 0.0f;
  out[static_cast<size_t>(i) * m + j] = cost;
}

// --- phase 2: one anti-diagonal ------------------------------------------
//
// d and ptr are (n+1) x (m+1), row-major, with the boundary row and column
// already filled by the host. `i_lo` is the first row index on this diagonal
// and `count` is how many cells it has.
__global__ void wavefront_kernel(float* __restrict__ d,
                                 int8_t* __restrict__ ptr,
                                 const float* __restrict__ sub,
                                 int k, int i_lo, int count,
                                 int n, int m) {
  const int t = blockIdx.x * blockDim.x + threadIdx.x;
  if (t >= count) return;

  const int i = i_lo + t;
  const int j = k - i;
  const size_t stride = static_cast<size_t>(m) + 1;
  const size_t here = static_cast<size_t>(i) * stride + j;

  const float diag = d[here - stride - 1] + sub[static_cast<size_t>(i - 1) * m + (j - 1)];
  const float up = d[here - stride] + kGapCost;
  const float left = d[here - 1] + kGapCost;

  float best = diag;
  int8_t move = 0;
  if (up < best) { best = up; move = 1; }
  if (left < best) { best = left; move = 2; }

  d[here] = best;
  ptr[here] = move;
}

// --- phase 2, banded ------------------------------------------------------
//
// A caption probe measuring drift within an operational SLO never needs a path
// that wanders more than `band` cells from the diagonal: a caption stream that
// far out of correspondence is a source loss or a wrong-language delivery, and
// both are separate metrics with their own detectors. Restricting the recurrence
// to the band turns the O(n*m) matrix into O((n+m) * band), which is the
// difference between a probe fleet that can sweep the slice matrix every ten
// seconds and one that cannot.
//
// Cells outside the band keep +inf, so a path is never silently allowed to leave
// it - the alignment cost simply reflects that the band was too narrow, and the
// host widens it and retries.
__global__ void wavefront_band_kernel(float* __restrict__ d,
                                      int8_t* __restrict__ ptr,
                                      const float* __restrict__ sub,
                                      int k, int i_lo, int count,
                                      int n, int m, int band) {
  const int t = blockIdx.x * blockDim.x + threadIdx.x;
  if (t >= count) return;

  const int i = i_lo + t;
  const int j = k - i;
  if (abs(i - j) > band) return;

  const size_t stride = static_cast<size_t>(m) + 1;
  const size_t here = static_cast<size_t>(i) * stride + j;

  const float diag = d[here - stride - 1] + sub[static_cast<size_t>(i - 1) * m + (j - 1)];
  const float up = d[here - stride] + kGapCost;
  const float left = d[here - 1] + kGapCost;

  float best = diag;
  int8_t move = 0;
  if (up < best) { best = up; move = 1; }
  if (left < best) { best = left; move = 2; }

  d[here] = best;
  ptr[here] = move;
}

// --- host-side driver -----------------------------------------------------
//
// Launches every anti-diagonal in order on one stream. The traceback stays on
// the host: it is O(n + m), strictly serial, and copying the pointer matrix back
// costs less than a device-side serial walk.
void raccord_align(const int32_t* ref_id, const int32_t* hyp_id,
                       const int32_t* ref_pre, const int32_t* hyp_pre,
                       const int8_t* ref_long, const int8_t* hyp_long,
                       float* d, int8_t* ptr, float* sub,
                       int n, int m, int band, cudaStream_t stream) {
  const dim3 block2d(16, 16);
  const dim3 grid2d((m + block2d.x - 1) / block2d.x, (n + block2d.y - 1) / block2d.y);
  substitution_kernel<<<grid2d, block2d, 0, stream>>>(
      ref_id, hyp_id, ref_pre, hyp_pre, ref_long, hyp_long, sub, n, m);

  constexpr int kBlock = 128;
  for (int k = 2; k <= n + m; ++k) {
    const int i_lo = max(1, k - m);
    const int count = min(n, k - 1) - i_lo + 1;
    if (count <= 0) continue;
    const int grid = (count + kBlock - 1) / kBlock;
    if (band > 0) {
      wavefront_band_kernel<<<grid, kBlock, 0, stream>>>(
          d, ptr, sub, k, i_lo, count, n, m, band);
    } else {
      wavefront_kernel<<<grid, kBlock, 0, stream>>>(d, ptr, sub, k, i_lo, count, n, m);
    }
  }
}

}  // extern "C"
