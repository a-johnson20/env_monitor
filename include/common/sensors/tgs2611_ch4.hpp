#pragma once

#include <math.h>

namespace tgs2611 {

// ---------------------------------------------------------------------------
// Shah 2023 (AMT 16:3391) Eq.(6) population-representative defaults.
// Table 4 — all five characterised sensors:
//   LSCE001: a = 26.3 ppm, α = 0.368, R² = 0.9997, RMSE_ratio = 0.0038
//   LSCE003: a = 23.2 ppm, α = 0.357, R² = 0.9997, RMSE_ratio = 0.0041
//   LSCE005: a = 30.2 ppm, α = 0.461, R² = 0.9993, RMSE_ratio = 0.0068
//   LSCE007: a = 31.3 ppm, α = 0.439, R² = 0.9993, RMSE_ratio = 0.0065
//   LSCE009: a = 24.7 ppm, α = 0.502, R² = 0.9986, RMSE_ratio = 0.0099
// Defaults use LSCE009 (best match to measured data at 20ppm and 100ppm):
//   20ppm measured: Shah mean → 27.6ppm (+38%),  LSCE009 → 20.7ppm (+3.5%)
//   100ppm measured: Shah mean → 139ppm (+39%),   LSCE009 → 90.6ppm (-9.4%)
// ---------------------------------------------------------------------------
constexpr float SHAH_A_DEFAULT     = 24.7f;   // ppm   — LSCE009 (mean was 27.14)
constexpr float SHAH_ALPHA_DEFAULT = 0.502f;  // dimensionless — LSCE009 (mean was 0.4254)

// Total R_L (kΩ): ISL22317 digipot wiper=0 (0 kΩ) + 1 kΩ safety resistor.
// Update if the wiper code changes.
constexpr float R_L_KOHM = 1.0f;

// Sensor supply voltage (V) — must match Figaro Vs = 5.0 V.
constexpr float VS_V = 5.0f;

// ---------------------------------------------------------------------------
// calc_rs_kohm
// Compute sensor resistance Rs (kΩ) from the measured load-resistor voltage
// V_RL and the total load resistance R_L_total_kohm.
//
// Circuit topology:
//   +5 V ──[Rs sensor]──> AIN0 ──[R_L_total]──> GND
//   V_RL = voltage at AIN0 = voltage across R_L
//   Rs = R_L × (Vs − V_RL) / V_RL
//
// Returns NAN for invalid inputs (zero or out-of-range voltage).
// ---------------------------------------------------------------------------
inline float calc_rs_kohm(float v_rl, float r_l_total_kohm = R_L_KOHM) {
    if (v_rl <= 0.0f || v_rl >= VS_V || r_l_total_kohm <= 0.0f) return NAN;
    return r_l_total_kohm * (VS_V - v_rl) / v_rl;
}

// ---------------------------------------------------------------------------
// calc_ppm_ch4_shah
// Shah 2023 Eq.(6) inverted — derive [CH4] (ppm) from resistance ratio.
//
// Forward model:  R / R2ppm = (1 + ([CH4] − 2 ppm) / a)^(−α)
// Inverted:       [CH4]     = 2 + a × ((Rs / R2ppm)^(−1/α) − 1)
//
// Parameters:
//   rs_kohm    — sensor resistance at current gas conditions (kΩ)
//   r2ppm_kohm — reference resistance at 2 ppm CH4, ambient T/H2O (kΩ),
//                stored in EEPROM after running in background air for ≥24 h
//   a          — characteristic CH4 mole fraction (ppm)
//   alpha      — power exponent (dimensionless)
//
// Returns ppm CH4 (clamped to ≥ 0), or NAN for invalid inputs.
// ---------------------------------------------------------------------------
inline float calc_ppm_ch4_shah(float rs_kohm, float r2ppm_kohm,
                                float a = SHAH_A_DEFAULT,
                                float alpha = SHAH_ALPHA_DEFAULT) {
    if (isnan(rs_kohm) || isnan(r2ppm_kohm)) return NAN;
    if (r2ppm_kohm <= 0.0f || rs_kohm <= 0.0f || alpha <= 0.0f || a <= 0.0f) return NAN;
    float ratio = rs_kohm / r2ppm_kohm;
    // Rs > R2ppm by more than 10% implies [CH4] < 2 ppm — not physically meaningful
    if (ratio > 1.1f) return NAN;
    float ppm = 2.0f + a * (powf(ratio, -1.0f / alpha) - 1.0f);
    return (ppm < 0.0f) ? 0.0f : ppm;
}

} // namespace tgs2611
