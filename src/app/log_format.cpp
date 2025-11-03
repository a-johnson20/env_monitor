#include "app/log_format.hpp"
#include "app/time_utils.hpp"

namespace {

static inline uint8_t normalize_yy_from_rv3028(int y_raw) {
  if (y_raw >= 0 && y_raw <= 99) return (uint8_t)y_raw;
  int cand = y_raw - 48;                  // fix accidental +'0'
  if (cand >= 0 && cand <= 99) return (uint8_t)cand;
  return (uint8_t)(y_raw % 100);
}


static void format_date_into(char* out, size_t n, bool rtc_present, RV3028& rtc) {
  struct tm lt{};
  if (now_local_tm(lt, rtc_present, &rtc)) {
    snprintf(out, n, "%04d%02d%02d", lt.tm_year + 1900, lt.tm_mon + 1, lt.tm_mday);
  } else {
    snprintf(out, n, "nodate");
  }
}

} // anon

namespace logfmt {

String current_log_path(bool rtc_present, RV3028& rtc) {
  char d[16];
  format_date_into(d, sizeof(d), rtc_present, rtc);
  String p = "/logs/";
  p += d;
  p += ".csv";
  return p;
}

String make_header(size_t n_scd4x,
                   size_t n_trhp,
                   size_t n_tgs2611,
                   size_t n_tgs2616)
{
  String h = "timestamp";

  // SCD4x
  for (size_t i = 0; i < n_scd4x; ++i) {
    h += ",scd4x_"; h += String(i+1); h += "_co2";
    h += ",scd4x_"; h += String(i+1); h += "_t";
    h += ",scd4x_"; h += String(i+1); h += "_rh";
  }

  // TRHP per-station averages
  for (size_t i = 0; i < n_trhp; ++i) {
    h += ",sht45_";   h += String(i+1); h += "_t_avg";
    h += ",sht45_";   h += String(i+1); h += "_rh_avg";
    h += ",tmp117_";  h += String(i+1); h += "_t_avg";
    h += ",lps22df_"; h += String(i+1); h += "_p_avg";
    h += ",lps22df_"; h += String(i+1); h += "_t_avg";
  }

  // TGS per-probe averages
  for (size_t i = 0; i < n_tgs2611; ++i) {
    h += ",tgs2611_"; h += String(i+1); h += "_raw_avg";
    h += ",tgs2611_"; h += String(i+1); h += "_v_avg";
  }
  for (size_t i = 0; i < n_tgs2616; ++i) {
    h += ",tgs2616_"; h += String(i+1); h += "_raw_avg";
    h += ",tgs2616_"; h += String(i+1); h += "_v_avg";
  }

  return h;
}

} // namespace logfmt
