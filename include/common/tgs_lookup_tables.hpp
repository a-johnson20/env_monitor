#pragma once

#include <Arduino.h>

#include "common/calib/tgs_calibration.hpp"

struct CalEntry { uint16_t id; uint8_t wiper; };

// Digipot values scaled from Figaro 5% LEL R_L table, anchored so ID10 = 1kΩ total R_L (digipot=0).
// Scale factor = 1.0 / 2.32 = 0.431. IDs 1-10 all floor at digipot=0 (scaled R_L < 1kΩ).
// Resistance should be 1k lower than total desired resistance, as digipot in series with 1k safety resistor
static const CalEntry TGS2611_CAL[] = {
  {1,   wiper_code_for_kohms(0.00f)},  // Figaro 0.976k * 0.431 = 0.42k < 1k floor
  {2,   wiper_code_for_kohms(0.00f)},  // Figaro 1.07k  * 0.431 = 0.46k < 1k floor
  {3,   wiper_code_for_kohms(0.00f)},  // Figaro 1.18k  * 0.431 = 0.51k < 1k floor
  {4,   wiper_code_for_kohms(0.00f)},  // Figaro 1.30k  * 0.431 = 0.56k < 1k floor
  {5,   wiper_code_for_kohms(0.00f)},  // Figaro 1.43k  * 0.431 = 0.62k < 1k floor
  {6,   wiper_code_for_kohms(0.00f)},  // Figaro 1.58k  * 0.431 = 0.68k < 1k floor
  {7,   wiper_code_for_kohms(0.00f)},  // Figaro 1.74k  * 0.431 = 0.75k < 1k floor
  {8,   wiper_code_for_kohms(0.00f)},  // Figaro 1.91k  * 0.431 = 0.82k < 1k floor
  {9,   wiper_code_for_kohms(0.00f)},  // Figaro 2.10k  * 0.431 = 0.91k < 1k floor
  {10,  wiper_code_for_kohms(0.00f)},  // Figaro 2.32k  * 0.431 = 1.00k → digipot=0 (anchor)
  {11,  wiper_code_for_kohms(0.10f)},  // Figaro 2.55k  * 0.431 = 1.10k
  {12,  wiper_code_for_kohms(0.21f)},  // Figaro 2.80k  * 0.431 = 1.21k
  {13,  wiper_code_for_kohms(0.33f)},  // Figaro 3.09k  * 0.431 = 1.33k
  {14,  wiper_code_for_kohms(0.47f)},  // Figaro 3.40k  * 0.431 = 1.47k
  {15,  wiper_code_for_kohms(0.61f)},  // Figaro 3.74k  * 0.431 = 1.61k
  {16,  wiper_code_for_kohms(0.78f)},  // Figaro 4.12k  * 0.431 = 1.78k
  {17,  wiper_code_for_kohms(0.95f)},  // Figaro 4.53k  * 0.431 = 1.95k
  {18,  wiper_code_for_kohms(1.15f)},  // Figaro 4.99k  * 0.431 = 2.15k
  {19,  wiper_code_for_kohms(1.37f)},  // Figaro 5.49k  * 0.431 = 2.37k
  {20,  wiper_code_for_kohms(1.60f)},  // Figaro 6.04k  * 0.431 = 2.60k
  {21,  wiper_code_for_kohms(1.87f)},  // Figaro 6.65k  * 0.431 = 2.87k
  {22,  wiper_code_for_kohms(2.16f)},  // Figaro 7.32k  * 0.431 = 3.16k
  {23,  wiper_code_for_kohms(2.47f)},  // Figaro 8.06k  * 0.431 = 3.47k
  {24,  wiper_code_for_kohms(3.82f)},  // Figaro 8.87k  * 0.431 = 3.82k
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
