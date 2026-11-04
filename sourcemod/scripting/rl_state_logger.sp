/**
 * RL State Logger v2
 * Writes game state to addons/sourcemod/logs/rl_state.json once per second (game time)
 * for the external Python RL Director.
 *
 * v2 changes:
 *  - Special infected are counted by m_zombieClass on infected-team players. (v1 searched for
 *    entity classnames like "hunter", which never exist in L4D2, so special_count was always 0.)
 *  - Adds per-class special counts, tank/witch/common counts, map flow progress, safe-area flag.
 *  - Per-survivor alive/incap flags, real temp health, primary-weapon clip + reserve ammo, flow.
 *  - Atomic-ish write (temp file + rename) so the reader never sees a half-written file.
 */
#pragma semicolon 1
#pragma newdecls required

#include <sourcemod>
#include <sdktools>
#include <left4dhooks>

#define TEAM_SURVIVOR 2
#define TEAM_INFECTED 3

public Plugin myinfo = {
    name = "RL State Logger",
    author = "RL Director Project",
    description = "Writes game state to a file for external RL agent",
    version = "2.0",
    url = ""
};

Handle g_hTimer = null;
char g_sPath[PLATFORM_MAX_PATH];
char g_sTmpPath[PLATFORM_MAX_PATH];

public void OnPluginStart()
{
    BuildPath(Path_SM, g_sPath, sizeof(g_sPath), "logs/rl_state.json");
    BuildPath(Path_SM, g_sTmpPath, sizeof(g_sTmpPath), "logs/rl_state.json.tmp");
    LogMessage("RL State Logger v2 loaded.");
}

public void OnMapStart()
{
    // TIMER_FLAG_NO_MAPCHANGE timers are freed by SourceMod on map change; just drop the handle.
    g_hTimer = CreateTimer(1.0, Timer_LogState, _, TIMER_REPEAT | TIMER_FLAG_NO_MAPCHANGE);
    LogMessage("RL State Logger timer started on new map.");
}

public void OnMapEnd()
{
    g_hTimer = null;
}

public void OnPluginEnd()
{
    // Late unload mid-map: stop the timer.
    if (g_hTimer != null) {
        delete g_hTimer;
    }
}

int GetPrimaryAmmo(int client, int &reserve)
{
    reserve = 0;
    int weapon = GetPlayerWeaponSlot(client, 0);
    if (weapon == -1 || !IsValidEntity(weapon))
        return 0;

    int clip = GetEntProp(weapon, Prop_Send, "m_iClip1");
    int ammoType = GetEntProp(weapon, Prop_Send, "m_iPrimaryAmmoType");
    if (ammoType >= 0)
        reserve = GetEntProp(client, Prop_Send, "m_iAmmo", _, ammoType);
    return clip < 0 ? 0 : clip;
}

int CountByClassname(const char[] classname)
{
    int count = 0;
    int entity = -1;
    while ((entity = FindEntityByClassname(entity, classname)) != -1)
        count++;
    return count;
}

public Action Timer_LogState(Handle timer)
{
    char buffer[4096];
    int pos = 0;

    float maxFlow = L4D2Direct_GetMapMaxFlowDistance();

    pos += FormatEx(buffer[pos], sizeof(buffer) - pos, "{\"survivors\":[");

    bool first = true;
    // Per-class counts for living special infected (index = L4D2ZombieClassType)
    int classCount[10];

    for (int i = 1; i <= MaxClients; i++)
    {
        if (!IsClientInGame(i))
            continue;

        int team = GetClientTeam(i);

        if (team == TEAM_INFECTED)
        {
            if (IsPlayerAlive(i) && !L4D_IsPlayerGhost(i))
            {
                int zc = GetEntProp(i, Prop_Send, "m_zombieClass");
                if (zc >= 0 && zc < sizeof(classCount))
                    classCount[zc]++;
            }
            continue;
        }

        if (team != TEAM_SURVIVOR)
            continue;

        float origin[3];
        GetClientAbsOrigin(i, origin);

        bool alive = IsPlayerAlive(i);
        bool incap = alive && L4D_IsPlayerIncapacitated(i);
        int health = alive ? GetClientHealth(i) : 0;
        int tempHealth = alive ? L4D_GetPlayerTempHealth(i) : 0;
        if (tempHealth < 0) tempHealth = 0;

        int reserve;
        int clip = alive ? GetPrimaryAmmo(i, reserve) : 0;
        if (!alive) reserve = 0;

        float flow = alive ? L4D2Direct_GetFlowDistance(i) : 0.0;

        if (!first) pos += FormatEx(buffer[pos], sizeof(buffer) - pos, ",");
        first = false;

        pos += FormatEx(buffer[pos], sizeof(buffer) - pos,
            "{\"health\":%d,\"temp_health\":%d,\"alive\":%d,\"incap\":%d,\"x\":%.1f,\"y\":%.1f,\"z\":%.1f,\"ammo\":%d,\"reserve_ammo\":%d,\"flow\":%.1f}",
            health, tempHealth, alive ? 1 : 0, incap ? 1 : 0,
            origin[0], origin[1], origin[2], clip, reserve, flow);
    }

    int specials = 0;
    for (int zc = 1; zc <= 6; zc++)
        specials += classCount[zc];
    int tanks = classCount[8];
    int witches = CountByClassname("witch");
    int commons = CountByClassname("infected");

    float furthest = L4D2_GetFurthestSurvivorFlow();
    float progress = maxFlow > 0.0 ? furthest / maxFlow : 0.0;

    char map[64];
    GetCurrentMap(map, sizeof(map));

    // special_count keeps v1 semantics (specials + tanks + witches) for backwards compatibility.
    pos += FormatEx(buffer[pos], sizeof(buffer) - pos,
        "],\"special_count\":%d,\"specials\":{\"smoker\":%d,\"boomer\":%d,\"hunter\":%d,\"spitter\":%d,\"jockey\":%d,\"charger\":%d},\"si_count\":%d,\"tank_count\":%d,\"witch_count\":%d,\"common_count\":%d,\"flow\":%.1f,\"max_flow\":%.1f,\"progress\":%.4f,\"left_safe_area\":%d,\"map\":\"%s\",\"time\":%.1f}",
        specials + tanks + witches,
        classCount[1], classCount[2], classCount[3], classCount[4], classCount[5], classCount[6],
        specials, tanks, witches, commons,
        furthest, maxFlow, progress,
        L4D_HasAnySurvivorLeftSafeArea() ? 1 : 0,
        map, GetGameTime());

    File file = OpenFile(g_sTmpPath, "w");
    if (file != null)
    {
        file.WriteLine(buffer);
        delete file;
        // Windows rename() won't overwrite, so delete first; the reader retries on a missing file.
        DeleteFile(g_sPath);
        RenameFile(g_sPath, g_sTmpPath);
    }

    return Plugin_Continue;
}
