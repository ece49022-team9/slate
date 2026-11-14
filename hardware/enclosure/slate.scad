// part: "rear" and "front_print" are the two printable shells, "assembly" shows them with the electronics,
// and "clash" is the overlap between the shells and the electronics, which is empty when everything fits.
part = "assembly";
cut_at_x = 0;
nudge = [0, 0, 0];

wall = 2;
floor_t = 1.6;
face_t = 1.6;
corner_r = 8;
edge_chamfer = 1.2;
seam_chamfer = 0.3;
lip_h = 1.6;
tongue_t = 0.85;
fit = 0.25;
shell_screw_len = 10;
shell_screw_inset = 6;
$fn = 64;

// Main board, from hardware/ESP32_PRELIM_DESIGN.kicad_pcb.
// u is KiCad y and v is KiCad x, both from the board's top-left corner, so USB-C faces down.
pcb = [40, 66, 1.6];
pcb_holes = [[2.49, 2.49], [37.49, 2.49], [28.09, 57.59]];
pcb_screws = [pcb_holes[0], pcb_holes[1]];
antenna_overhang = 6.01;
usb_u = 11.09;
usb_shell = [8.94, 7.35, 3.26];
socket_u = 24.69;
socket_v = 62.97;
socket_h = 8.51;
switch_u = 35.41;
switch_v = 27.42;
through_hole_tail = 1.8;

// Adafruit 1431 OLED breakout, from Adafruit's Eagle board and the UG-2828GDEDF11 panel drawing.
// Origin is the corner at the flex end, seen from the glass side.
display_pcb = [43.18, 41.91, 1.6];
display_holes = [[2.54, 2.54], [40.64, 2.54], [2.54, 39.37], [40.64, 39.37]];
display_header = [21.59, 40.386];
display_header_h = 2.54;
glass = [33.8, 34, 2];
glass_origin = [4.69, 2.1];
active_centre = [21.59, 20.5];
window = 29.2;

// Adafruit 258 cell at its maximum size.
battery = [35.5, 62.2, 5.2];
battery_slack = 0.6;

// Same Sky CMS-28528N-L152A.
speaker_d = 28;
speaker_h = 5.2;
speaker_ear_span = 37.6;
speaker_ear_pitch = 32;

// Adafruit 3492 PDM microphone breakout.
mic_board = [14, 12.8, 2.8];
mic_pitch = 31;

// Vybronics VG0832022D.
motor = [8, 3.25];

bottom_clear = 7.5;
top_clear = 13.1;
pcb_x = wall + antenna_overhang + 1;
pcb_y = wall + bottom_clear;
case_w = 2 * (pcb_x + socket_u);
case_h = pcb_y + pcb[1] + top_clear + wall;

pcb_z = floor_t + battery[2] + battery_slack + through_hole_tail + 0.5;
pcb_top = pcb_z + pcb[2];
usb_x = pcb_x + usb_u;
usb_z = pcb_top + 1.7;
usb_open = [14.4, 8];
usb_rim = 1.4;
usb_tunnel_end = pcb_y - 0.4;
seam_z = usb_z + usb_open[1] / 2 + usb_rim + 0.2;

display_x = pcb_x + socket_u - display_header[0];
display_y = pcb_y + socket_v - display_header[1];
display_back = pcb_top + socket_h + display_header_h;
display_front = display_back + display_pcb[2];
face_z = display_front + glass[2] + 0.45;
case_d = face_z + face_t;

speaker_c = [case_w / 2, pcb_y + 7];
mic_y = case_h - wall - 0.2 - mic_board[1] / 2;
mic_xs = [case_w / 2 - mic_pitch / 2, case_w / 2 + mic_pitch / 2];
battery_u = (pcb[0] - battery[0]) / 2 - 0.25;
battery_v = 5.4;
battery_pocket = [battery[0] + 0.5, battery[1] + 0.4];
screws = [
    [shell_screw_inset, shell_screw_inset],
    [case_w - shell_screw_inset, shell_screw_inset],
    [shell_screw_inset, case_h - shell_screw_inset],
    [case_w - shell_screw_inset, case_h - shell_screw_inset],
];
screw_seat_z = seam_z + 6 - shell_screw_len;

assert(case_w <= 68 && case_h <= 96, "Outline is larger than the 68 x 96 mm target");
assert(screw_seat_z >= floor_t + 1, "Shell screws are too long for the rear shell");
assert(speaker_c[1] - speaker_d / 2 >= wall + 0.3, "Speaker hits the bottom wall");
assert(speaker_c[1] + speaker_d / 2 <= display_y - 1.3, "Speaker hits the display flex");
assert(mic_y - mic_board[1] / 2 >= display_y + display_pcb[1] + 0.8, "Mics hit the display");

module rr(size, r) {
    offset(r = r) offset(delta = -r) square(size);
}

module outline(inset = 0) {
    translate([inset, inset]) rr([case_w - 2 * inset, case_h - 2 * inset], max(corner_r - inset, 0.6));
}

module slab(z0, z1, inset = 0, c0 = 0, c1 = 0) {
    hull() {
        if (c0 > 0) translate([0, 0, z0]) linear_extrude(0.01) outline(inset + c0);
        translate([0, 0, z0 + c0]) linear_extrude(z1 - z0 - c0 - c1) outline(inset);
        if (c1 > 0) translate([0, 0, z1 - 0.01]) linear_extrude(0.01) outline(inset + c1);
    }
}

module on_pcb(u, v) {
    translate([pcb_x + u, pcb_y + v]) children();
}

module along_y(y0, y1) {
    translate([0, y1, 0]) rotate([90, 0, 0]) linear_extrude(y1 - y0) children();
}

module usb_profile(grow = 0) {
    offset(r = grow) translate([-usb_open[0] / 2, usb_z - usb_open[1] / 2]) rr(usb_open, 3);
}

module usb_pocket_body() {
    translate([usb_x, 0, 0]) along_y(wall - 0.01, usb_tunnel_end) hull() {
        usb_profile(usb_rim);
        translate([-usb_open[0] / 2 - usb_rim, floor_t - 0.01]) square([usb_open[0] + 2 * usb_rim, 1]);
    }
}

module usb_pocket_void() {
    translate([usb_x, 0, 0]) {
        along_y(-1, usb_tunnel_end + 1) usb_profile();
        hull() {
            along_y(-0.02, -0.01) usb_profile(0.8);
            along_y(0.8, 0.81) usb_profile();
        }
    }
}

module rear_features() {
    rail_z = floor_t + 4;
    for (p = screws) translate(p) cylinder(r = 4, h = seam_z);
    for (h = pcb_screws) on_pcb(h[0], h[1]) cylinder(d = 5.2, h = pcb_z);
    on_pcb(-2.4, 52) cube([3.8, 8, pcb_z]);
    on_pcb(pcb[0] - 1.4, 57.5) cube([3.8, 7.5, pcb_z]);
    on_pcb(battery_u - 1.2, 20) cube([1.2, 30, rail_z]);
    on_pcb(battery_u + battery_pocket[0], 14) cube([1.2, 42, rail_z]);
    on_pcb(battery_u + 8, battery_v + battery_pocket[1]) cube([battery_pocket[0] - 16, 1.2, rail_z]);
    translate([display_x + display_pcb[0] - 5, display_y + 0.3, 0]) hull() {
        cube([4.5, 2.4, display_back - 0.15]);
        translate([0, -5, 0]) cube([4.5, 5, floor_t]);
    }
    usb_pocket_body();
}

module rear_cuts() {
    for (p = screws) translate([p[0], p[1], -1]) {
        cylinder(d = 2.4, h = seam_z + lip_h + 2);
        cylinder(d = 4.6, h = screw_seat_z + 1);
    }
    for (h = pcb_screws) on_pcb(h[0], h[1]) translate([0, 0, pcb_z - 7]) cylinder(d = 1.7, h = 8);
    on_pcb(-2.6, 13) translate([0, 0, floor_t - 0.4]) cylinder(d = motor[0] + 0.6, h = 1);
    hull() for (u = [pcb[0] + 2.5, pcb[0] + 8]) on_pcb(u, switch_v) translate([0, 0, -1]) cylinder(d = 3, h = floor_t + 2);
    usb_pocket_void();
}

module rear_shell() {
    difference() {
        union() {
            difference() {
                slab(0, seam_z, 0, edge_chamfer, seam_chamfer);
                slab(floor_t, seam_z + 1, wall);
            }
            difference() {
                slab(seam_z - 0.01, seam_z + lip_h, wall - tongue_t);
                slab(seam_z - 1, seam_z + lip_h + 1, wall);
            }
            intersection() {
                slab(0, display_back, wall - 0.01);
                rear_features();
            }
        }
        rear_cuts();
    }
}

module speaker_fences() {
    translate([speaker_c[0], speaker_c[1], face_z - 3.5]) for (a = [24, 116, 204, 296])
        rotate([0, 0, a]) rotate_extrude(angle = 40) translate([speaker_d / 2 + 0.3, 0]) square([1.2, 3.51]);
    for (s = [-1, 1]) translate([speaker_c[0] + s * speaker_ear_pitch / 2, speaker_c[1], face_z - 3])
        cylinder(d = 2.6, h = 3.01);
}

module mic_fences() {
    for (x = mic_xs) translate([x - mic_board[0] / 2 - 1.2, mic_y - mic_board[1] / 2 - 1.2, face_z - 2.4]) difference() {
        cube([mic_board[0] + 2.4, mic_board[1] + 1.2, 2.41]);
        translate([1, 1, -1]) cube([mic_board[0] + 0.4, mic_board[1] + 2, 5]);
        translate([mic_board[0] / 2 - 3.8, -1, -1]) cube([10, 3, 5]);
    }
}

module display_bosses() {
    for (h = display_holes) translate([display_x + h[0], display_y + h[1]]) {
        translate([0, 0, display_front + 0.4]) cylinder(d = 3.4, h = face_z - display_front);
        translate([0, 0, display_front - 1.3]) hull() {
            cylinder(d = 1.5, h = 0.1);
            translate([0, 0, 0.4]) cylinder(d = 2.1, h = 1.4);
        }
    }
}

module front_features() {
    intersection() {
        slab(seam_z, face_z + 0.01, wall + fit);
        for (p = screws) translate(p) cylinder(r = 4.2, h = case_d);
    }
    display_bosses();
    speaker_fences();
    mic_fences();
}

module grille(centre, radius, pitch, hole) {
    rows = ceil(radius / pitch) + 1;
    for (i = [-rows:rows], j = [-rows:rows]) {
        x = (i + (abs(j) % 2) / 2) * pitch;
        y = j * pitch * sqrt(3) / 2;
        if (norm([x, y]) <= radius) translate([centre[0] + x, centre[1] + y, face_z - 1])
            cylinder(d = hole, h = face_t + 2, $fn = 24);
    }
}

module front_cuts() {
    translate([display_x + active_centre[0] - window / 2, display_y + active_centre[1] - window / 2]) {
        translate([0, 0, face_z - 1]) linear_extrude(face_t + 1) rr([window, window], 1);
        hull() {
            translate([0, 0, case_d - 0.9]) linear_extrude(0.01) rr([window, window], 1);
            translate([0, 0, case_d]) linear_extrude(0.01) offset(r = 0.9) rr([window, window], 1);
        }
    }
    grille(speaker_c, 11.3, 2.9, 1.9);
    for (x = mic_xs) grille([x, mic_y], 2.3, 2.2, 1.2);
    for (p = screws) translate([p[0], p[1], seam_z - 0.1]) cylinder(d = 1.7, h = 7.1);
}

module front_shell() {
    difference() {
        intersection() {
            slab(seam_z, case_d, 0, seam_chamfer, edge_chamfer);
            union() {
                difference() {
                    slab(seam_z, case_d);
                    slab(seam_z - 1, face_z, wall);
                    slab(seam_z - 1, seam_z + lip_h + 0.3, wall - tongue_t - fit);
                }
                front_features();
            }
        }
        front_cuts();
    }
}

module board() {
    translate([pcb_x, pcb_y, pcb_z]) {
        difference() {
            cube(pcb);
            for (h = pcb_holes) translate([h[0], h[1], -1]) cylinder(d = 2.2, h = 4);
        }
        translate([0, 0, pcb[2]]) {
            translate([-antenna_overhang, 30.99, 0]) cube([25.5, 18, 0.8]);
            translate([1.5, 31.8, 0]) cube([17.2, 16.4, 3.1]);
            translate([usb_u - usb_shell[0] / 2, 0.01, 0.06]) cube(usb_shell);
            translate([23.34, -1.51, 0]) cube([5.9, 7.6, 4.86]);
            translate([31.34, 30.94, 0]) cube([7.62, 11.9, 4.86]);
            translate([31.34, 43.89, 0]) cube([7.62, 11.9, 4.86]);
            translate([10.72, 61.69, 0]) cube([27.95, 2.55, socket_h]);
            translate([1.41, 59.85, 0]) cube([7.66, 5.2, 3.41]);
            translate([36.16, 25.32, 0]) cube([2.8, 4.2, 1.42]);
            translate([switch_u, 26.17, 0.3]) cube([0.76, 2.5, 0.9]);
            translate([2, 2, 0]) cube([36, 24, 2.2]);
        }
        translate([0, 0, -through_hole_tail]) {
            for (u = [25.29, 27.29]) translate([u, 4.74, 0]) cylinder(d = 0.8, h = 2);
            for (i = [0:4], v0 = [32.89, 45.84]) translate([32.7, v0 + 2 * i, 0]) cylinder(d = 0.8, h = 2);
            for (i = [0:10]) translate([11.99 + 2.54 * i, socket_v, 0.3]) cylinder(d = 0.8, h = 2);
        }
    }
}

module plugs() {
    translate([pcb_x, pcb_y, pcb_top]) {
        translate([23.6, -6.2, 0.3]) cube([5.4, 4.7, 4.5]);
        translate([38.96, 31.4, 0.3]) cube([5, 11, 4.5]);
        translate([38.96, 44.3, 0.3]) cube([5, 11, 4.5]);
        translate([2, 65.05, 0.3]) cube([6.5, 5.5, 3]);
    }
}

module cell() {
    on_pcb(battery_u + 0.25, battery_v + 0.2) translate([0, 0, floor_t]) cube(battery);
}

module display() {
    translate([display_x, display_y, display_back]) {
        difference() {
            linear_extrude(display_pcb[2]) rr([display_pcb[0], display_pcb[1]], 2.54);
            for (h = display_holes) translate([h[0], h[1], -1]) cylinder(d = 2.5, h = 4);
        }
        translate([glass_origin[0], glass_origin[1], display_pcb[2]]) cube(glass);
        translate([display_header[0] - 9.3, -1, -1]) cube([18.6, glass_origin[1] + 1.5, display_pcb[2] + glass[2] + 1]);
        translate([display_header[0] - 13.97, display_header[1] - 1.27, -display_header_h]) cube([27.94, 2.54, display_header_h]);
        translate([8, 8, -1.9]) cube([28, 26, 1.9]);
    }
}

module audio() {
    translate([speaker_c[0], speaker_c[1], face_z - speaker_h]) cylinder(d = speaker_d, h = speaker_h);
    translate([speaker_c[0], speaker_c[1], face_z - 1]) difference() {
        hull() for (s = [-1, 1]) translate([s * (speaker_ear_span / 2 - 3), 0, 0]) cylinder(d = 6, h = 1);
        for (s = [-1, 1]) translate([s * speaker_ear_pitch / 2, 0, -1]) cylinder(d = 3, h = 3);
    }
    for (x = mic_xs) translate([x, mic_y, face_z]) {
        translate([-mic_board[0] / 2, -mic_board[1] / 2, -mic_board[2]]) cube([mic_board[0], mic_board[1], 1.6]);
        translate([-2, -1.5, -1.2]) cube([4, 3, 1.2]);
    }
    on_pcb(-2.6, 13) translate([0, 0, floor_t - 0.4]) cylinder(d = motor[0], h = motor[1]);
}

module parts() {
    board();
    plugs();
    cell();
    display();
    audio();
}

module cut() {
    if (cut_at_x > 0) intersection() {
        children();
        translate([cut_at_x, -50, -50]) cube([200, 300, 200]);
    } else children();
}

cut() {
    if (part == "rear") rear_shell();
    else if (part == "front") front_shell();
    else if (part == "front_print") translate([0, case_h, case_d]) rotate([180, 0, 0]) front_shell();
    else if (part == "board") board();
    else if (part == "plugs") plugs();
    else if (part == "cell") cell();
    else if (part == "display") display();
    else if (part == "audio") audio();
    else if (part == "clash") intersection() {
        union() {
            rear_shell();
            front_shell();
        }
        translate(nudge) parts();
    }
    else {
        color("#2f3437") rear_shell();
        color("#e9e4d8", 0.6) front_shell();
        color("#2e6b4a") board();
        color("#f2f2f2") plugs();
        color("#9aa3ad") cell();
        color("#1b3a8a") display();
        color("#c9a227") audio();
    }
}

