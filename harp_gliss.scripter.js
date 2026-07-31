/*
 * Harp Gliss — MainStage Scripter script
 *
 * Turns any keypress into a diatonic gliss (harp-style scale sweep).
 * The incoming note-on is ABSORBED (not passed through). In its place
 * the script schedules a scale of notes from Start Note to End Note in
 * the configured Key + Scale, distributed evenly across Duration ms,
 * with a linear velocity ramp from Start Velocity to End Velocity.
 *
 * A harp is diatonic — glisses follow the notes of a scale in the given
 * key, not a chromatic run. Choose Scale = "Pentatonic Major" for the
 * classic C-major-black-key ("celtic") gliss, or "Major"/"Minor" for a
 * seven-note diatonic sweep.
 *
 * Parameters (visible in Scripter's parameter panel — map any of these
 * to Screen Controls if you want per-patch overrides without swapping
 * channel strips):
 *
 *   Key            — tonic of the scale (C..B)
 *   Scale          — major, natural minor, dorian, mixolydian, pentatonic
 *   Start Note     — lowest / first note of the gliss (MIDI 0-127)
 *   End Note       — highest / last note; if lower than Start, plays
 *                    descending
 *   Duration ms    — total time from first note-on to last
 *   Start Velocity — velocity of the first note (default 60)
 *   End Velocity   — velocity of the last note (default 100); linear ramp
 *   Note Length ms — how long each note sustains
 *
 * Non-note events (CC, pitch bend, aftertouch, program change) pass
 * through untouched. Note-offs from the trigger key are also absorbed
 * because every scheduled gliss note carries its own note-off.
 */

var PluginParameters = [
    { name: "Key", type: "menu",
      valueStrings: ["C", "C#", "D", "D#", "E", "F",
                     "F#", "G", "G#", "A", "A#", "B"],
      defaultValue: 0 },
    { name: "Scale", type: "menu",
      valueStrings: ["Major", "Natural Minor", "Dorian", "Mixolydian",
                     "Pentatonic Major"],
      defaultValue: 0 },
    { name: "Start Note", type: "lin",
      minValue: 0, maxValue: 127, numberOfSteps: 127,
      defaultValue: 36 },   // C2
    { name: "End Note", type: "lin",
      minValue: 0, maxValue: 127, numberOfSteps: 127,
      defaultValue: 96 },   // C7
    { name: "Duration ms", type: "lin",
      minValue: 50, maxValue: 3000, numberOfSteps: 2950,
      defaultValue: 500, unit: "ms" },
    { name: "Start Velocity", type: "lin",
      minValue: 1, maxValue: 127, numberOfSteps: 126,
      defaultValue: 60 },
    { name: "End Velocity", type: "lin",
      minValue: 1, maxValue: 127, numberOfSteps: 126,
      defaultValue: 100 },
    { name: "Note Length ms", type: "lin",
      minValue: 20, maxValue: 3000, numberOfSteps: 2980,
      defaultValue: 400, unit: "ms" }
];

// Pitch-class sets (semitones from tonic) for each scale option.
var SCALES = [
    [0, 2, 4, 5, 7, 9, 11],    // Major
    [0, 2, 3, 5, 7, 8, 10],    // Natural Minor
    [0, 2, 3, 5, 7, 9, 10],    // Dorian
    [0, 2, 4, 5, 7, 9, 10],    // Mixolydian
    [0, 2, 4, 7, 9]            // Pentatonic Major
];

function scaleNotes(keyPc, scale, lo, hi) {
    // Every MIDI note in [lo, hi] that belongs to (keyPc, scale).
    var out = [];
    for (var n = lo; n <= hi; n++) {
        var pc = ((n - keyPc) % 12 + 12) % 12;
        if (scale.indexOf(pc) >= 0) out.push(n);
    }
    return out;
}

function HandleMIDI(event) {
    if (event instanceof NoteOn) {
        var keyPc  = GetParameter("Key");
        var scale  = SCALES[GetParameter("Scale")];
        var startN = Math.round(GetParameter("Start Note"));
        var endN   = Math.round(GetParameter("End Note"));
        var duration = GetParameter("Duration ms");
        var vStart = GetParameter("Start Velocity");
        var vEnd   = GetParameter("End Velocity");
        var noteLen = GetParameter("Note Length ms");

        var lo = Math.min(startN, endN);
        var hi = Math.max(startN, endN);
        var ascending = endN >= startN;
        var notes = scaleNotes(keyPc, scale, lo, hi);
        if (!ascending) notes.reverse();
        if (notes.length === 0) return;   // no notes in range

        var span = Math.max(notes.length - 1, 1);
        var stepMs = duration / span;

        for (var i = 0; i < notes.length; i++) {
            var t = i / span;
            var vel = Math.max(1, Math.min(127,
                Math.round(vStart + (vEnd - vStart) * t)));
            var on = new NoteOn();
            on.pitch = notes[i];
            on.velocity = vel;
            on.sendAfterMilliseconds(i * stepMs);

            var off = new NoteOff();
            off.pitch = notes[i];
            off.velocity = 0;
            off.sendAfterMilliseconds(i * stepMs + noteLen);
        }
        // Absorb the trigger note — do NOT event.send()
    }
    else if (event instanceof NoteOff) {
        // Absorb; each scheduled gliss note has its own note-off.
    }
    else {
        // Pass CC / pitch bend / program change / etc. through.
        event.send();
    }
}
