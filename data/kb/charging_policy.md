# Fleet charging policy and best practice

## Time-of-use tariffs and when to charge
Charging operators in the network price energy by time of day. Night off-peak (22:00-06:00) is roughly 40 % cheaper than the default rate and the evening peak (18:00-22:00) is about 35 % more expensive. Vehicles parked overnight should charge off-peak using the planner, which also spreads power across slots to reduce battery wear. Typical savings versus plugging in and charging immediately are 30-60 %.

## DC fast charging and battery wear
DC fast charging is convenient for mid-shift top-ups but heats the pack and accelerates degradation, especially above 80 % state of charge where charging power tapers. Policy: use DC only when the remaining route needs it, stop at 80 %, and prefer AC for overnight charging. Vehicles with a DC fast share above 40 % degrade measurably faster.

## Low range and reachable chargers
Rated range shown on dashboards overestimates real range in traffic, heat with air-conditioning, and with payload. VoltPath predicts range from recent consumption and checks the road distance to the nearest charger with a free connector; when range is below that distance plus a 15 % margin, a LOW_RANGE_NO_CHARGER alert is raised. Action: reroute the driver to the suggested charger immediately or dispatch roadside assistance.

## Target state of charge
For daily operations charge to 80 %; charge to 100 % only before long routes and depart soon after. Avoid leaving vehicles below 10 % for long periods.
