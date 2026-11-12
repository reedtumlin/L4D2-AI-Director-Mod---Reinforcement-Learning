/**
 * RL Director Tools
 * RCON-callable helpers for the external RL Director.
 *
 *   sm_rl_spawn <smoker|boomer|hunter|spitter|jockey|charger|tank|witch> [count]
 *       Spawns infected at a valid infected spawn position near the lead survivor.
 *       Works on a dedicated server with no human client (unlike z_spawn_old ... auto).
 *
 *   sm_rl_leave_saferoom [distance=600]
 *       Teleports all living survivors to a nav area on the main path ~distance flow units ahead
 *       of the lead survivor and outside the start checkpoint. This trips the Director's
 *       "survivor left safe area" logic (and Left 4 Bots 2's auto-lead) with no human client.
 *
 *   sm_rl_auto_spectate <0|1>   (cvar, default 0)
 *       Demo mode. Every human client is kept on the spectator team (so the survivors stay
 *       all-bot, exactly like training) and follows the lead survivor in third person
 *       (sm_rl_spectate_mode 5, default) or first person (sm_rl_spectate_mode 4).
 *       The view is only re-targeted when the current target stops being a living survivor,
 *       so the spectator can still click to cycle between bots. The camera is set through the
 *       engine's spec_mode command and re-synced every second if the server-side view drifts.
 *
 *   sm_rl_specinfo
 *       Debug: prints each human spectator's server-side observer mode, target, movetype and
 *       position. "dist" should stay near 0 while spectating; a large value means the server
 *       is culling the world from somewhere other than where the camera is.
 */
#pragma semicolon 1
#pragma newdecls required

#include <sourcemod>
#include <sdktools>
#include <left4dhooks>

public Plugin myinfo = {
    name = "RL Director Tools",
    author = "RL Director Project",
    description = "Spawn/control helpers for the external RL Director",
    version = "1.2",
    url = ""
};

#define TEAM_SPECTATOR 1
#define TEAM_SURVIVOR 2
#define OBS_MODE_IN_EYE 4
#define OBS_MODE_CHASE 5

static const char g_sClassNames[][] = {
    "", "smoker", "boomer", "hunter", "spitter", "jockey", "charger", "witch", "tank"
};

#define SPEC_MAX_DRIFT 150.0

ConVar g_cvAutoSpectate;
ConVar g_cvSpectateMode;

public void OnPluginStart()
{
    RegAdminCmd("sm_rl_spawn", Command_Spawn, ADMFLAG_ROOT, "sm_rl_spawn <class> [count]");
    RegAdminCmd("sm_rl_leave_saferoom", Command_LeaveSaferoom, ADMFLAG_ROOT, "sm_rl_leave_saferoom [distance]");
    g_cvAutoSpectate = CreateConVar("sm_rl_auto_spectate", "0",
        "1 = keep human clients spectating the survivor bots (demo mode)", 0, true, 0.0, true, 1.0);
    g_cvSpectateMode = CreateConVar("sm_rl_spectate_mode", "5",
        "Demo camera: 5 = third person (chase), 4 = first person", 0, true, 4.0, true, 5.0);
    RegAdminCmd("sm_rl_specinfo", Command_SpecInfo, ADMFLAG_ROOT, "Print each spectator's server-side view state");
}

public Action Command_SpecInfo(int client, int args)
{
    for (int i = 1; i <= MaxClients; i++)
    {
        if (!IsClientInGame(i) || IsFakeClient(i))
            continue;
        float org[3], eye[3], tEye[3];
        GetClientAbsOrigin(i, org);
        GetClientEyePosition(i, eye);
        int mode = GetEntProp(i, Prop_Send, "m_iObserverMode");
        int target = GetEntPropEnt(i, Prop_Send, "m_hObserverTarget");
        int mt = view_as<int>(GetEntityMoveType(i));
        float dist = -1.0;
        if (target > 0 && target <= MaxClients && IsClientInGame(target))
        {
            GetClientEyePosition(target, tEye);
            dist = GetVectorDistance(eye, tEye);
        }
        ReplyToCommand(client, "[RL] spec %d team=%d mode=%d target=%d movetype=%d eye=(%.0f %.0f %.0f) org=(%.0f %.0f %.0f) target_eye=(%.0f %.0f %.0f) dist=%.0f viewent=%d",
            i, GetClientTeam(i), mode, target, mt, eye[0], eye[1], eye[2], org[0], org[1], org[2],
            tEye[0], tEye[1], tEye[2], dist, GetEntPropEnt(i, Prop_Data, "m_hViewEntity"));
    }
    return Plugin_Handled;
}

public void OnMapStart()
{
    // TIMER_FLAG_NO_MAPCHANGE timers are freed automatically on map change.
    CreateTimer(1.0, Timer_Spectate, _, TIMER_REPEAT | TIMER_FLAG_NO_MAPCHANGE);
}

bool IsLivingSurvivor(int client)
{
    return client > 0 && client <= MaxClients && IsClientInGame(client)
        && GetClientTeam(client) == TEAM_SURVIVOR && IsPlayerAlive(client);
}

int PickSpectateTarget()
{
    int lead = L4D_GetHighestFlowSurvivor();
    if (IsLivingSurvivor(lead))
        return lead;
    for (int i = 1; i <= MaxClients; i++)
    {
        if (IsLivingSurvivor(i))
            return i;
    }
    return -1;
}

public Action Timer_Spectate(Handle timer)
{
    if (!g_cvAutoSpectate.BoolValue)
        return Plugin_Continue;

    for (int i = 1; i <= MaxClients; i++)
    {
        if (!IsClientInGame(i) || IsFakeClient(i))
            continue;

        if (GetClientTeam(i) != TEAM_SPECTATOR)
        {
            // Coop puts joining humans onto the survivor team (taking over a bot);
            // moving them to spectator hands the survivor back to the AI.
            ChangeClientTeam(i, TEAM_SPECTATOR);
            continue; // set the camera next tick, once the team change has settled
        }

        int current = GetEntPropEnt(i, Prop_Send, "m_hObserverTarget");
        if (IsLivingSurvivor(current) && SpectatorInSync(i, current))
            continue; // healthy; also leaves a bot the viewer picked by clicking alone

        int target = IsLivingSurvivor(current) ? current : PickSpectateTarget();
        if (target == -1)
            continue;

        // Go through the engine's own spectator commands instead of writing the observer
        // netprops directly. Writing m_iObserverMode only changes what the client draws: the
        // server keeps the spectator as a walking player parked where they joined, so it
        // culls entities and closes area portals from that spot (invisible infected, "void"
        // rooms). spec_mode runs CBasePlayer::SetObserverMode, which switches the server to
        // MOVETYPE_OBSERVER so the spectator's server-side position follows the target.
        FakeClientCommand(i, "spec_mode %d", g_cvSpectateMode.IntValue);
        if (GetEntPropEnt(i, Prop_Send, "m_hObserverTarget") != target)
            SetEntPropEnt(i, Prop_Send, "m_hObserverTarget", target);
    }
    return Plugin_Continue;
}

// True when the server-side observer state matches what the viewer sees: a first- or third-person
// view of the target, observer movetype, and a server-side position on top of the target (that
// position decides which entities and areas get networked to the viewer). Either camera mode
// counts as in sync, so the viewer can still press Space to switch between them.
bool SpectatorInSync(int client, int target)
{
    int mode = GetEntProp(client, Prop_Send, "m_iObserverMode");
    if (mode != OBS_MODE_IN_EYE && mode != OBS_MODE_CHASE)
        return false;
    if (GetEntityMoveType(client) != MOVETYPE_OBSERVER)
        return false;
    float a[3], b[3];
    GetClientAbsOrigin(client, a);
    GetClientAbsOrigin(target, b);
    return GetVectorDistance(a, b) <= SPEC_MAX_DRIFT;
}

public Action Command_LeaveSaferoom(int client, int args)
{
    float distance = 600.0;
    if (args >= 1)
    {
        char buf[16];
        GetCmdArg(1, buf, sizeof(buf));
        distance = StringToFloat(buf);
    }

    if (L4D_HasAnySurvivorLeftSafeArea())
    {
        ReplyToCommand(client, "[RL] already_left");
        return Plugin_Handled;
    }

    float startFlow = L4D2_GetFurthestSurvivorFlow();
    if (startFlow < 0.0) startFlow = 0.0;
    float targetFlow = startFlow + distance;

    ArrayList areas = new ArrayList();
    L4D_GetAllNavAreas(areas);

    Address best = Address_Null;
    float bestDiff = 999999.0;
    float center[3], size[3];
    for (int i = 0; i < areas.Length; i++)
    {
        Address area = areas.Get(i);
        float flow = L4D2Direct_GetTerrorNavAreaFlow(area);
        if (flow <= startFlow)
            continue; // also skips off-path areas (flow < 0)
        float diff = FloatAbs(flow - targetFlow);
        if (diff >= bestDiff)
            continue;

        L4D_GetNavAreaSize(area, size);
        if (size[0] < 50.0 || size[1] < 50.0)
            continue; // tiny areas are often on ledges/stairs
        if (L4D_NavArea_IsBlocked(area, 2, true))
            continue;
        L4D_GetNavAreaCenter(area, center);
        if (L4D_IsPositionInFirstCheckpoint(center))
            continue;

        best = area;
        bestDiff = diff;
    }
    delete areas;

    if (best == Address_Null)
    {
        ReplyToCommand(client, "[RL] no_area (start_flow=%.0f)", startFlow);
        return Plugin_Handled;
    }

    L4D_GetNavAreaCenter(best, center);
    center[2] += 10.0;

    int moved = 0;
    float pos[3];
    for (int i = 1; i <= MaxClients; i++)
    {
        if (!IsClientInGame(i) || GetClientTeam(i) != 2 || !IsPlayerAlive(i))
            continue;
        // Small offsets so survivors don't spawn inside each other.
        pos = center;
        pos[0] += float((moved % 2) * 40 - 20);
        pos[1] += float((moved / 2) * 40 - 20);
        TeleportEntity(i, pos, NULL_VECTOR, view_as<float>({0.0, 0.0, 0.0}));
        moved++;
    }

    ReplyToCommand(client, "[RL] moved %d survivors to flow %.0f (start %.0f) pos %.0f %.0f %.0f",
        moved, L4D2Direct_GetTerrorNavAreaFlow(best), startFlow, center[0], center[1], center[2]);
    return Plugin_Handled;
}

int ParseClass(const char[] name)
{
    for (int i = 1; i < sizeof(g_sClassNames); i++)
    {
        if (StrEqual(name, g_sClassNames[i], false))
            return i;
    }
    return -1;
}

public Action Command_Spawn(int client, int args)
{
    if (args < 1)
    {
        ReplyToCommand(client, "[RL] Usage: sm_rl_spawn <class> [count]");
        return Plugin_Handled;
    }

    char name[32];
    GetCmdArg(1, name, sizeof(name));
    int zc = ParseClass(name);
    if (zc == -1)
    {
        ReplyToCommand(client, "[RL] Unknown class '%s'", name);
        return Plugin_Handled;
    }

    int count = 1;
    if (args >= 2)
    {
        char buf[8];
        GetCmdArg(2, buf, sizeof(buf));
        count = StringToInt(buf);
        if (count < 1) count = 1;
        if (count > 8) count = 8;
    }

    int anchor = L4D_GetHighestFlowSurvivor();
    if (anchor < 1 || anchor > MaxClients || !IsClientInGame(anchor))
        anchor = 0; // random area

    // Witches use the generic SI search area; class 7 is not a valid PZ search class.
    int searchClass = (zc == 7) ? 1 : zc;

    int spawned = 0;
    float pos[3], ang[3];
    for (int n = 0; n < count; n++)
    {
        if (!L4D_GetRandomPZSpawnPosition(anchor, searchClass, 10, pos))
            continue;

        int ent;
        if (zc == 8)      ent = L4D2_SpawnTank(pos, ang);
        else if (zc == 7) ent = L4D2_SpawnWitch(pos, ang);
        else              ent = L4D2_SpawnSpecial(zc, pos, ang);

        if (ent > 0) spawned++;
    }

    ReplyToCommand(client, "[RL] spawned %d/%d %s", spawned, count, g_sClassNames[zc]);
    return Plugin_Handled;
}
