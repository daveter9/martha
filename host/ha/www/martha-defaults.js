// Default period of HA's date pickers (energy-date-selection) on the martha dashboards.
// HA keeps the last chosen period per collection in localStorage and starts the picker there;
// this resource sets ours each time the frontend loads, so every picker starts on its default
// (user's request, 2026-11-11). The collection keys are those in host/ha/dashboards.py
// (COLLECTIONS); the values are HA's presets.
const MARTHA_DEFAULT_PERIODS = {
  energy_martha_solar: "this_week",
  energy_martha_power: "today",
  energy_martha_use: "this_week",
  energy_martha_gas: "this_week",
  energy_martha_dhw: "today",
};

for (const [key, preset] of Object.entries(MARTHA_DEFAULT_PERIODS)) {
  try {
    localStorage.setItem(`energy-default-period-_${key}`, preset);
  } catch (e) {
    // No storage (private window): HA starts on today.
  }
}
