# Battery state of health

## What state of health (SoH) means
SoH is the pack's usable capacity relative to new. Commercial fleets usually plan replacement or redeployment when SoH reaches 80 %. NMC packs typically lose about 2 percentage points per year under normal use and LFP packs about 1.3; heavy DC fast charging, heat and high daily mileage increase the rate.

## Rapid degradation and anomalies
VoltPath compares each pack's degradation slope with its model cohort using a robust z-score (median and median absolute deviation). A score above 3.5 marks an anomaly - often a defective cell module, a cooling fault or abusive charging. Anomalous packs should be inspected while under warranty and moved to shorter routes.

## Similar vehicles
Vehicles whose degradation curve and usage profile look alike (vector similarity) often share a root cause - the same charging habits, route type or production batch. Use the similar-vehicles view to find fleet-wide issues from one bad pack.
