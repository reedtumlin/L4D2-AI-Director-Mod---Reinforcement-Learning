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
    version = "1.0",
    url = ""
};

static const char g_sClassNames[][] = {
    "", "smoker", "boomer", "hunter", "spitter", "jockey", "charger", "witch", "tank"
};

public void OnPluginStart()
{
    RegAdminCmd("sm_rl_spawn", Command_Spawn, ADMFLAG_ROOT, "sm_rl_spawn <class> [count]");
    RegAdminCmd("sm_rl_leave_saferoom", Command_LeaveSaferoom, ADMFLAG_ROOT, "sm_rl_leave_saferoom [distance]");
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
