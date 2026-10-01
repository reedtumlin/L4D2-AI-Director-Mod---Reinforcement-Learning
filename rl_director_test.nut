// rl_director_test.nut
// Correctly modifies the Director's options by getting a reference to them.

printl("=== RL Director Test Script Loaded (v2) ===");

// Get a reference to the active DirectorOptions table
local dopts = DirectorScript.GetDirectorOptions();

// Now, set the values on that reference.
// We use the <- operator to add or update these keys.
dopts.SmokerLimit <- 0;
dopts.BoomerLimit <- 0;
dopts.HunterLimit <- 0;
dopts.SpitterLimit <- 0;
dopts.JockeyLimit <- 0;
dopts.ChargerLimit <- 0;

// This is the correct key for blocking Tanks and Witches.
// It is more powerful than the old 'ProhibitBosses' flag.
dopts.DisallowThreatType <- 3; // 3 is a bitwise OR for ZOMBIE_TANK (1) and ZOMBIE_WITCH (2)

// To ensure specials don't spawn due to other settings,
// we can also set the max number of specials to 0.
dopts.MaxSpecials <- 0;