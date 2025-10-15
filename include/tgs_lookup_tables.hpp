#pragma once
#include <Arduino.h>

struct CalEntry { uint16_t id; uint8_t wiper; };

// ==== FILL THESE WITH YOUR REAL MAPPINGS ====
// Keep separate if you like per-sensor defaults/handling
static const CalEntry TGS2611_CAL[] = {
  {1001, 82},
  {1002, 77},
  {1050, 90},
};
static const size_t N_TGS2611_CAL = sizeof(TGS2611_CAL)/sizeof(TGS2611_CAL[0]);

static const CalEntry TGS2616_CAL[] = {
  {2001, 64},
  {2002, 72},
};
static const size_t N_TGS2616_CAL = sizeof(TGS2616_CAL)/sizeof(TGS2616_CAL[0]);
