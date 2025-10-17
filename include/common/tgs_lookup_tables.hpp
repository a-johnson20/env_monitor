#pragma once
#include <Arduino.h>

// Project includes
#include "../src/common/calib/tgs_calibration.hpp"

struct CalEntry { uint16_t id; uint8_t wiper; };

// ==== FILL THESE WITH YOUR REAL MAPPINGS ====
// Keep separate if you like per-sensor defaults/handling
// Resistance should be 1k lower than total desired resistance, as digipot in series with 1k safety resistor
static const CalEntry TGS2611_CAL[] = {
  {1,   wiper_code_for_kohms(0.21f)},
  {2,   wiper_code_for_kohms(0.33f)},
  {3,   wiper_code_for_kohms(0.47f)},
  {4,   wiper_code_for_kohms(0.62f)},
  {5,   wiper_code_for_kohms(0.78f)},
  {6,   wiper_code_for_kohms(0.96f)},
  {7,   wiper_code_for_kohms(1.15f)},
  {8,   wiper_code_for_kohms(1.37f)},
  {9,   wiper_code_for_kohms(1.61f)},
  {10,  wiper_code_for_kohms(1.87f)},
  {11,  wiper_code_for_kohms(2.16f)},
  {12,  wiper_code_for_kohms(2.48f)},
  {13,  wiper_code_for_kohms(2.83f)},
  {14,  wiper_code_for_kohms(3.22f)},
  {15,  wiper_code_for_kohms(3.64f)},
  {16,  wiper_code_for_kohms(4.11f)},
  {17,  wiper_code_for_kohms(4.62f)},
  {18,  wiper_code_for_kohms(5.19f)},
  {19,  wiper_code_for_kohms(5.81f)},
  {20,  wiper_code_for_kohms(6.50f)},
  {21,  wiper_code_for_kohms(7.25f)},
  {22,  wiper_code_for_kohms(8.09f)},
  {23,  wiper_code_for_kohms(9.00f)},
  {24,  wiper_code_for_kohms(10.0f)},
};
static const size_t N_TGS2611_CAL = sizeof(TGS2611_CAL)/sizeof(TGS2611_CAL[0]);

static const CalEntry TGS2616_CAL[] = { // NEED TO SET THESE TO CORRECT IDS & VALUES
  {1,   wiper_code_for_kohms(5.00f)},
  {2,   wiper_code_for_kohms(5.00f)},
  {3,   wiper_code_for_kohms(5.00f)},
  {4,   wiper_code_for_kohms(5.00f)},
  {5,   wiper_code_for_kohms(5.00f)},
  {6,   wiper_code_for_kohms(5.00f)},
  {7,   wiper_code_for_kohms(5.00f)},
  {8,   wiper_code_for_kohms(5.00f)},
  {9,   wiper_code_for_kohms(5.00f)},
  {10,  wiper_code_for_kohms(5.00f)},
  {11,  wiper_code_for_kohms(5.00f)},
  {12,  wiper_code_for_kohms(5.00f)},
  {13,  wiper_code_for_kohms(5.00f)},
  {14,  wiper_code_for_kohms(5.00f)},
  {15,  wiper_code_for_kohms(5.00f)},
  {16,  wiper_code_for_kohms(5.00f)},
  {17,  wiper_code_for_kohms(5.00f)},
  {18,  wiper_code_for_kohms(5.00f)},
  {19,  wiper_code_for_kohms(5.00f)},
  {20,  wiper_code_for_kohms(5.00f)},
  {21,  wiper_code_for_kohms(5.00f)},
  {22,  wiper_code_for_kohms(5.00f)},
  {23,  wiper_code_for_kohms(5.00f)},
  {24,  wiper_code_for_kohms(5.00f)},
};
static const size_t N_TGS2616_CAL = sizeof(TGS2616_CAL)/sizeof(TGS2616_CAL[0]);
