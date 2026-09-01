"""Fixed alert-box identities: one DPS box plus four general-purpose Alert
Boxes that alert_rules can be assigned to (reaction_config["box"], see
alerts.py's AlertEngine._fire) so several rules can share one on-screen box
instead of each rule getting its own by default.

Deliberately a fixed, hardcoded set rather than user-creatable at runtime --
a box is a placement (positioned once via the native overlay's companion
app, see overlay-native/companion/main.cpp), not disposable per-rule
config. To add another box, add one entry here AND the matching entry in
main.cpp's kBoxes array -- the two are duplicated on purpose (companion is
a separate C++ binary with no shared config/DB access), matching how the
socket-path constants are already duplicated between companion and layer.
"""

BOXES = [
    {"key": "dps_meter", "name": "DPS"},
    {"key": "alert_box_1", "name": "Alert Box 1"},
    {"key": "alert_box_2", "name": "Alert Box 2"},
    {"key": "alert_box_3", "name": "Alert Box 3"},
    {"key": "alert_box_4", "name": "Alert Box 4"},
]
