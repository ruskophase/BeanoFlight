# Measured nozzle layouts

BeanoFlight 0.2 accepts the independent `pinkplane-nozzle-map/v2` file exported
by PinkPlane Align. It describes the intersections of nozzle axes with the
central plane, not the physical valve bodies. Camera calibration remains a
separate input and is not modified.

## Loading and reviewing

Open a recording and its metric calibration, then choose **Load nozzle map**
in BeanoFlight. Review and Simulation use the same loaded layout. Stop an
active run before changing layouts; a change requires a new analysis/run.
The display shows measured nozzle labels, actual zone widths and heights.

```bash
beano-flight /path/to/recording --nozzle-map /path/to/nozzle-map.json
```

The same `--nozzle-map` option is supported by `beano-system-test` for both
recorded and `--live` runs, and by the statistics-bundle command. Add the
option to your existing command with its usual recording/live-source arguments.

Omitting the option retains the original virtual layout: 21 uniform 5 mm
zones, normally 30 mm below the field of view. The GUI's **Use virtual layout**
button explicitly restores this mode. `--sorting-offset-mm` affects only the
virtual layout; it never shifts measured positions.

A selected map must match the active CamL homography checksum, intrinsic
binding, image dimensions, grid pitch and tracking transform. Saved metric
intersections are cross-checked against their undistorted pixel coordinates.
The loader requires an odd count of 3–21 nozzles, unique IDs and strictly
increasing physical x positions. Incompatible maps are rejected, not silently
replaced by virtual geometry. A failed GUI file selection leaves the previous
valid selection unchanged and displays an error. A calibration change that
invalidates the selected map disables analysis until resolved.

## Geometry, probabilities and timing

The integration uses the map's CamL tracking-coordinate intersections rather
than assuming its bottom-centre display coordinates are tracking coordinates.
The tool-to-plane offset correction is already in the exported intersections;
BeanoFlight does not apply it a second time.

Each zone extends horizontally to the midpoint between adjacent nozzle
centres. Each outer edge extends half the nearest centre-to-centre spacing.
Each nozzle retains its own measured y coordinate. Prediction therefore
calculates a separate crossing time, horizontal distribution, timing
uncertainty and zone probability at each nozzle's height. Already-passed
nozzles cannot be selected.

These probabilities describe the bean centre entering an assigned zone, not
the probability of successful pneumatic ejection. They do not model jet width,
bean dimensions, airflow or measured nozzle-position uncertainty. Events at
different heights can overlap, so their probabilities are neither normalized
nor added. The existing adjacent-pair policy can combine zones only when their
heights match; individually qualifying nozzles can still be selected together.

Each selected nozzle gets its own open/close window around its own predicted
arrival. Existing open-lead, close-lag and minimum-notice settings remain
independent of geometry. Virtual simulation and external output scheduling
both retain those individual windows. The legacy scalar prediction summary
uses the earliest future crossing for conservative downstream deadlines;
the trajectory display uses the most probable future nozzle's crossing.

## Output mapping and upgrade precautions

PinkPlane exports `valve_channel: null`. Such a map supports review and virtual
simulation immediately, but external actuation is blocked if any selected
nozzle lacks an explicit channel assignment. Geometry cannot establish wiring.

Before enabling external outputs, verify the rig's actual wiring and populate
`valve_channel` on a copy of the map: unique integers 0–20 are **output-mask bit
numbers, not GPIO numbers**. The current firmware convention maps these bits
to gate indices -10 through +10 (`gate_index = valve_channel - 10`). Nozzle IDs
and left-to-right order are not assumed to be electrical channel numbers.
Load the amended file as a new map; its checksum changes.

Restart Flight, Registry, Sorter and Actuator together after this software
upgrade. Measured multi-window output plans use a new v2 transport message;
older actuator software rejects that message instead of flattening the
windows. The existing firmware SCHEDULE command is used once per selected
nozzle, with one aggregate decision/result audit. No firmware flash is required
for this protocol change. The current hardware support remains LED-only;
this feature does not commission or authorize pneumatic-valve operation.

## Reproducibility

Each run session includes an immutable copy of the selected map, its SHA-256,
original path and derived zones in `settings.nozzle_layout`. Review exports
and statistics provenance also include the layout. Measured predictions and
decisions carry the map checksum; decisions retain individual nozzle timing
windows and channel assignments. Editing the source JSON does not alter an
already-loaded layout or a recorded session. Start a new run to use a changed
map, and use that run's saved layout when reproducing its results.
