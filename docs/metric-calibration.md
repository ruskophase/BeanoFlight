# Metric fall-plane calibration

PinkPlane's v2 camera-to-camera file includes an ordered rectangular grid of
mean CamL hole centres. BeanoFlight assigns `(column * 9.16, row * 9.16)` mm to
those centres and fits a full planar homography from undistorted CamL pixels to
millimetres.

After fitting, the transform is translated so the centre of the undistorted
CamL image is `(0, 0)` mm. Positive x points right in CamL and positive y points
down. The sorting line is parallel to the metric x axis and is placed 30 mm
below the mapped bottom-centre of the physical image.

A homography is preferable to one average pixels/mm scale because it handles
the residual projective change in scale and grid orientation across the image.
Intrinsic lens correction must already have been applied. BeanoFlight rejects
PinkPlane mappings in the native distorted coordinate domain.

Using the calibration currently stored in the Beano workspace and a 9.16 mm
pitch produced approximately:

| Quantity | Value |
| --- | ---: |
| CamL FoV width | 98 mm |
| CamL FoV height | 74 mm |
| Metric fit RMS | 0.039 mm |
| Metric fit maximum residual | 0.081 mm |
| Bottom-centre y | +37.2 mm |
| Virtual sorting-line y | +67.2 mm |

These residuals measure how closely the recorded centres agree with an ideal
regular grid. They do not include PinkPlane manufacturing tolerance, a warped
plate, inaccurate placement of the plate in the bean fall plane, or a bean
whose centre travels at a different camera depth. Those effects must later be
measured and added to prediction uncertainty.

The calibration object can be serialized as
`beanoflight-metric-plane/v1`; it includes the source calibration SHA-256 so a
metric result remains bound to the PinkPlane data from which it was derived.
