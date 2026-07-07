# LoRaWAN Uplink Payload Decoder

## Payload Layout (18 bytes)

| Bytes | Type    | Scaling | Parameter           | N/A Sentinel |
|-------|---------|---------|---------------------|--------------|
| 0–3   | uint32  | raw     | Unix UTC epoch      | 0            |
| 4–5   | uint16  | 1:1     | CO₂ ppm             | 0xFFFF       |
| 6–7   | uint16  | ×100    | RH %                | 0xFFFF       |
| 8–9   | int16   | ×100    | Temperature °C      | 0x8000       |
| 10–11 | uint16  | ×10     | Pressure hPa        | 0xFFFF       |
| 12–13 | uint16  | ×100    | CH₄ ppm             | 0xFFFF       |
| 14–15 | uint16  | 1:1     | TGS2611 raw ADC     | 0xFFFF       |
| 16–17 | uint16  | ×100    | SFM3505 air flow SLM| 0xFFFF       |

## Decoder Function (TTN / ChirpStack)

Paste this into your LoRaWAN network server's payload formatter:

```javascript
function decodeUplink(input) {
  var b = input.bytes;
  if (b.length < 18) return { errors: ["payload too short"] };

  // Use multiplication for MSB to avoid JS signed 32-bit overflow with <<
  var epoch = b[0] * 16777216 + b[1] * 65536 + b[2] * 256 + b[3];
  var co2   = (b[4]  << 8) | b[5];
  var rh    = (b[6]  << 8) | b[7];
  var temp  = (b[8]  << 8) | b[9];  if (temp  > 32767)  temp  -= 65536;
  var pres  = (b[10] << 8) | b[11];
  var ch4   = (b[12] << 8) | b[13];
  var raw   = (b[14] << 8) | b[15];
  var air   = (b[16] << 8) | b[17];

  return { data: {
    utc_epoch   : epoch,
    co2_ppm     : co2  === 0xFFFF  ? null : co2,
    rh_pct      : rh   === 0xFFFF  ? null : rh   / 100,
    temp_c      : temp === -32768  ? null : temp  / 100,
    pres_hpa    : pres === 0xFFFF  ? null : pres  / 10,
    ch4_ppm     : ch4  === 0xFFFF  ? null : ch4   / 100,
    tgs2611_raw : raw  === 0xFFFF  ? null : raw,
    air_slm     : air  === 0xFFFF  ? null : air   / 100
  }};
}
```

## Notes

- All values are **big-endian**.
- `0xFFFF` (65535) marks a sensor as unavailable for unsigned fields.
- `0x8000` (-32768) marks temperature as unavailable (signed int16 — the only field that can be negative).
- Temperature example: `2500` → 25.00 °C, `-500` → -5.00 °C.
- Air flow example: `1500` → 15.00 SLM (standard litres per minute).
- Raw ADC is the 12-bit value from the ADS1113 (range 0–2047 typically, fits in uint16).