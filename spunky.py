"""
Spunky Bot - An automated game server bot
http://www.spunkybot.de
Author: Alexander Kress

This program is released under the MIT License. See LICENSE for more details.

## About ##
Spunky Bot is a lightweight game server administration bot and RCON tool,
inspired by the eb2k9 bot by Shawn Haggard.
The purpose of Spunky Bot is to administrate an Urban Terror 4.1 / 4.2 server
and provide statistical data for players.

## Configuration ##
Modify the UrT server config as follows:
 * seta g_logsync "1"
 * seta g_loghits "1"
Modify the files '/conf/settings.conf' and '/conf/rules.conf'
Run the bot: python spunky.py
"""

__version__ = '1.1.2'


### IMPORTS
import re
import time
import sqlite3
import math
import textwrap
import urllib
import urllib2
import platform
import lib.pygeoip as pygeoip
import lib.schedule as schedule

from lib.rcon import Rcon
from lib.rules import Rules
from threading import RLock
from ConfigParser import ConfigParser


## CLASS TaskManager ###
class TaskManager(object):
    """
    Tasks
     - get RCON status
     - display alert messages
     - check warnings
     - check for spectators on full server
     - check player ping
    """
    def __init__(self, max_ping, num_kick_specs, rcon_dispatcher):
        """
        create a new instance of TaskManager

        @param max_ping: Maximum allowed ping
        @type  max_ping: Integer
        @param num_kick_specs: Number of players for kicking spectators on full server
        @type num_kick_specs: Integer
        @param rcon_dispatcher: The RCON instance
        @type  rcon_dispatcher: Instance
        """
        self.max_ping = max_ping
        self.num_kick_specs = num_kick_specs
        self.rcon_dispatcher = rcon_dispatcher

    def process(self):
        """
        start process
        """
        # get rcon status
        self.rcon_dispatcher.get_status()
        try:
            # check amount of warnings and kick player if needed, check for spectators
            self.check_warnings_and_specs()
            if self.max_ping > 0:
                # check for player with high ping
                self.check_ping()
        except Exception, err:
            print "%s: %s" % (err.__class__.__name__, err)

    def check_ping(self):
        """
        check ping of all players and set warning for high ping user
        """
        with players_lock:
            # rcon update status
            self.rcon_dispatcher.quake.rcon_update()
            for player in self.rcon_dispatcher.quake.players:
                # if ping is too high, increase warn counter, Admins or higher levels will not get the warning
                try:
                    ping_value = player.ping
                    gameplayer = game.players[player.num]
                except KeyError:
                    continue
                else:
                    if self.max_ping < ping_value < 999 and gameplayer.get_admin_role() < 40:
                        gameplayer.add_high_ping(ping_value)
                        game.rcon_tell(player.num, "^1WARNING ^7[^3%d^7]: ^7Your ping is too high [^4%d^7]. ^3The maximum allowed ping is %d." % (gameplayer.get_high_ping(), ping_value, self.max_ping), False)
                    else:
                        gameplayer.clear_high_ping()

    def check_warnings_and_specs(self):
        """
        - check warnings and kick players with too many warnings
        - check for spectators and set warning
        """
        with players_lock:
            # get number of connected players
            counter = len(game.players) - 1  # bot is counted as player

            for player in game.players.itervalues():
                player_name = player.get_name()
                player_num = player.get_player_num()
                player_admin_role = player.get_admin_role()
                # kick player with 3 warnings, Admins will never get kicked
                if player.get_warning() > 2 and player_admin_role < 40:
                    game.rcon_say("^7Player ^2%s ^7kicked, because of too many warnings" % player_name)
                    game.kick_player(player_num)
                # kick player with high ping after 3 warnings, Admins will never get kicked
                elif player.get_high_ping() > 2 and player_admin_role < 40:
                    game.rcon_say("^7Player ^2%s ^7kicked, ping was too high for this server ^7[^4%s^7]" % (player_name, player.get_ping_value()))
                    game.kick_player(player_num)
                # kick spectator after 3 warnings, Moderator or higher levels will not get kicked
                elif player.get_spec_warning() > 2 and player_admin_role < 20:
                    game.rcon_say("^7Player ^2%s ^7kicked, spectator too long on full server" % player_name)
                    game.kick_player(player_num)

                # warn player with 2 warnings, Admins will never get the alert warning
                if (player.get_warning() == 2 or player.get_spec_warning() == 2) and player_admin_role < 40:
                    game.rcon_say("^1ALERT: ^7Player ^2%s, ^7auto-kick from warnings if not cleared" % player_name)
                    # increase counter to kick player next cycle automatically
                    player.add_spec_warning()
                    player.add_warning()

                # check for spectators and set warning
                if self.num_kick_specs > 0:
                    # ignore player with name prefix GTV-
                    if 'GTV-' in player_name:
                        continue
                    # if player is spectator on full server, inform player and increase warn counter
                    # GTV or Moderator or higher levels will not get the warning
                    elif counter > self.num_kick_specs and player.get_team() == 3 and player_admin_role < 20 and player.get_time_joined() < (time.time() - 30) and player_num != 1022:
                        player.add_spec_warning()
                        game.rcon_tell(player_num, "^1WARNING ^7[^3%d^7]: ^7You are spectator too long on full server" % player.get_spec_warning(), False)
                    # reset spec warning
                    else:
                        player.clear_spec_warning()


### CLASS Log Parser ###
class LogParser(object):
    """
    log file parser
    """
    def __init__(self, file_name, server_port, verbose_mode, tk_autokick):
        """
        create a new instance of LogParser

        @param file_name: The full path of the games log file
        @type  file_name: String
        @param server_port: Port of the game server
        @type  server_port: String
        @param verbose_mode: Enable or disable verbose mode to print debug messages
        @type  verbose_mode: Boolean
        @param tk_autokick: Enable or disable autokick for team killing
        @type  tk_autokick: Boolean
        """
        # hit zone support for UrT > 4.2.013
        self.hit_points = {0: "HEAD", 1: "HEAD", 2: "HELMET", 3: "TORSO", 4: "VEST", 5: "LEFT_ARM", 6: "RIGHT_ARM", 7: "GROIN", 8: "BUTT", 9: "LEFT_UPPER_LEG", 10: "RIGHT_UPPER_LEG", 11: "LEFT_LOWER_LEG", 12: "RIGHT_LOWER_LEG", 13: "LEFT_FOOT", 14: "RIGHT_FOOT"}
        self.hit_item = {1: "UT_MOD_KNIFE", 2: "UT_MOD_BERETTA", 3: "UT_MOD_DEAGLE", 4: "UT_MOD_SPAS", 5: "UT_MOD_MP5K", 6: "UT_MOD_UMP45", 8: "UT_MOD_LR300", 9: "UT_MOD_G36", 10: "UT_MOD_PSG1", 14: "UT_MOD_SR8", 15: "UT_MOD_AK103", 17: "UT_MOD_NEGEV", 19: "UT_MOD_M4", 20: "UT_MOD_GLOCK", 21: "UT_MOD_COLT1911", 22: "UT_MOD_MAC11", 23: "UT_MOD_BLED", 24: "UT_MOD_KICKED", 25: "UT_MOD_KNIFE_THROWN"}
        self.death_cause = {1: "MOD_WATER", 3: "MOD_LAVA", 5: "UT_MOD_TELEFRAG", 6: "MOD_FALLING", 7: "UT_MOD_SUICIDE", 9: "MOD_TRIGGER_HURT", 10: "MOD_CHANGE_TEAM", 12: "UT_MOD_KNIFE", 13: "UT_MOD_KNIFE_THROWN", 14: "UT_MOD_BERETTA", 15: "UT_MOD_KNIFE_DEAGLE", 16: "UT_MOD_SPAS", 17: "UT_MOD_UMP45", 18: "UT_MOD_MP5K", 19: "UT_MOD_LR300", 20: "UT_MOD_G36", 21: "UT_MOD_PSG1", 22: "UT_MOD_HK69", 23: "UT_MOD_BLED", 24: "UT_MOD_KICKED", 25: "UT_MOD_HEGRENADE", 28: "UT_MOD_SR8", 30: "UT_MOD_AK103", 31: "UT_MOD_SPLODED", 32: "UT_MOD_SLAPPED", 34: "UT_MOD_BOMBED", 35: "UT_MOD_NUKED", 36: "UT_MOD_NEGEV", 37: "UT_MOD_HK69_HIT", 38: "UT_MOD_M4", 39: "UT_MOD_GLOCK", 40: "UT_MOD_COLT1911", 41: "UT_MOD_MAC11", 42: "UT_MOD_FLAG"}

        # RCON commands for the different admin roles
        self.user_cmds = ['forgiveall, forgiveprev', 'hs', 'register', 'spree', 'stats', 'teams', 'time', 'xlrstats']
        self.mod_cmds = self.user_cmds + ['country', 'leveltest', 'list', 'nextmap', 'mute', 'shuffleteams', 'warn']
        self.admin_cmds = self.mod_cmds + ['admins', 'aliases', 'bigtext', 'force', 'kick', 'nuke', 'say', 'tempban', 'warnclear']
        self.fulladmin_cmds = self.admin_cmds + ['ban', 'ci', 'scream', 'slap', 'swap', 'version', 'veto']
        self.senioradmin_cmds = self.fulladmin_cmds + ['banlist', 'cyclemap', 'kill', 'kiss', 'map', 'maps', 'maprestart', 'moon', 'permban', 'putgroup', 'setnextmap', 'unban', 'ungroup']

        # alphabetic sort of the commands
        self.mod_cmds.sort()
        self.admin_cmds.sort()
        self.fulladmin_cmds.sort()
        self.senioradmin_cmds.sort()

        # open game log file
        self.log_file = open(file_name, 'r')
        # go to the end of the file
        self.log_file.seek(0, 2)
        self.ffa_lms_gametype = False
        self.ctf_gametype = False
        self.ts_gametype = False
        self.ts_do_team_balance = False
        self.allow_cmd_teams = True
        self.urt42_modversion = True
        # enable/disable debug output
        self.verbose = verbose_mode
        # enable/disable autokick for team killing
        self.tk_autokick = tk_autokick
        # enable/disable option to get Head Admin by checking existence of head admin in database
        curs.execute("SELECT COUNT(*) FROM `xlrstats` WHERE `admin_role` = 100")
        self.iamgod = True if curs.fetchone()[0] < 1 else False
        # Master Server
        self.base_url = 'http://master.spunkybot.de'
        # Heartbeat packet
        data = {'v': __version__, 'p': server_port, 'o': platform.platform()}
        values = urllib.urlencode(data)
        self.ping_url = '%s/ping.php?%s' % (self.base_url, values)

    def find_game_start(self):
        """
        find InitGame start
        """
        seek_amount = 768
        # search within the specified range for the InitGame message
        start_pos = self.log_file.tell() - seek_amount
        end_pos = start_pos + seek_amount
        self.log_file.seek(start_pos)
        game_start = False
        while not game_start:
            while self.log_file:
                line = self.log_file.readline()
                msg = re.search(r"(\d+:\d+)\s([A-Za-z]+:)", line)
                if msg is not None and msg.group(2) == 'InitGame:':
                    game_start = True
                    if 'g_modversion\\4.1' in line:
                        # hit zone support for UrT 4.1
                        self.hit_points = {0: "HEAD", 1: "HELMET", 2: "TORSO", 3: "KEVLAR", 4: "ARMS", 5: "LEGS", 6: "BODY"}
                        self.hit_item = {1: "UT_MOD_KNIFE", 2: "UT_MOD_BERETTA", 3: "UT_MOD_DEAGLE", 4: "UT_MOD_SPAS", 5: "UT_MOD_MP5K", 6: "UT_MOD_UMP45", 8: "UT_MOD_LR300", 9: "UT_MOD_G36", 10: "UT_MOD_PSG1", 14: "UT_MOD_SR8", 15: "UT_MOD_AK103", 17: "UT_MOD_NEGEV", 19: "UT_MOD_M4", 21: "UT_MOD_KICKED", 22: "UT_MOD_KNIFE_THROWN"}
                        self.death_cause = {1: "MOD_WATER", 3: "MOD_LAVA", 5: "UT_MOD_TELEFRAG", 6: "MOD_FALLING", 7: "UT_MOD_SUICIDE", 9: "MOD_TRIGGER_HURT", 10: "MOD_CHANGE_TEAM", 12: "UT_MOD_KNIFE", 13: "UT_MOD_KNIFE_THROWN", 14: "UT_MOD_BERETTA", 15: "UT_MOD_KNIFE_DEAGLE", 16: "UT_MOD_SPAS", 17: "UT_MOD_UMP45", 18: "UT_MOD_MP5K", 19: "UT_MOD_LR300", 20: "UT_MOD_G36", 21: "UT_MOD_PSG1", 22: "UT_MOD_HK69", 23: "UT_MOD_BLED", 24: "UT_MOD_KICKED", 25: "UT_MOD_HEGRENADE", 28: "UT_MOD_SR8", 30: "UT_MOD_AK103", 31: "UT_MOD_SPLODED", 32: "UT_MOD_SLAPPED", 33: "UT_MOD_BOMBED", 34: "UT_MOD_NUKED", 35: "UT_MOD_NEGEV", 37: "UT_MOD_HK69_HIT", 38: "UT_MOD_M4", 39: "UT_MOD_FLAG", 40: "UT_MOD_GOOMBA"}
                        self.urt42_modversion = False
                        self.debug("Game modversion 4.1 detected")
                    if 'g_gametype\\0' in line or 'g_gametype\\1' in line or 'g_gametype\\9' in line:
                        # disable teamkill event and some commands for FFA (0), LMS (1) and Jump (9) mode
                        self.ffa_lms_gametype = True
                    elif 'g_gametype\\7' in line:
                        self.ctf_gametype = True
                    elif 'g_gametype\\4' in line:
                        self.ts_gametype = True
                if self.log_file.tell() > end_pos:
                    break
                elif len(line) == 0:
                    break
            if self.log_file.tell() < seek_amount:
                self.log_file.seek(0, 0)
            else:
                cur_pos = start_pos - seek_amount
                end_pos = start_pos
                start_pos = cur_pos
                if start_pos < 0:
                    start_pos = 0
                self.log_file.seek(start_pos)

    def read_log(self):
        """
        read the logfile
        """
        task_frequency = CONFIG.getint('bot', 'task_frequency')
        if task_frequency > 0:
            # create instance of TaskManager
            tasks = TaskManager(CONFIG.getint('bot', 'max_ping'), CONFIG.getint('bot', 'kick_spec_full_server'), game.rcon_handle)
            # schedule the task
            if task_frequency < 10:
                # avoid flooding with too less delay
                schedule.every(10).seconds.do(tasks.process)
            else:
                schedule.every(task_frequency).seconds.do(tasks.process)
        # schedule the task
        schedule.every(12).hours.do(self.send_heartbeat)

        self.find_game_start()
        self.log_file.seek(0, 2)
        while self.log_file:
            schedule.run_pending()
            line = self.log_file.readline()
            if len(line) != 0:
                self.parse_line(line)
            else:
                if not game.live:
                    game.go_live()
                time.sleep(.125)

    def send_heartbeat(self):
        """
        send heartbeat packet
        """
        try:
            urllib2.urlopen(self.ping_url)
        except urllib2.URLError:
            pass

    def parse_line(self, string):
        """
        parse the logfile and search for specific action
        """
        line = string[7:]
        tmp = line.split(":", 1)
        try:
            line = tmp[1].strip()
            if tmp is not None:
                if tmp[0].lstrip() == 'InitGame':
                    self.ffa_lms_gametype = True if ('g_gametype\\0' in line or 'g_gametype\\1' in line or 'g_gametype\\9' in line) else False
                    self.ctf_gametype = True if 'g_gametype\\7' in line else False
                    self.ts_gametype = True if 'g_gametype\\4' in line else False
                    self.debug("Starting game...")
                    game.new_game()
                elif tmp[0].lstrip() == 'Warmup':
                    with players_lock:
                        for player in game.players.itervalues():
                            player.reset()
                    game.set_current_map()
                    self.allow_cmd_teams = True
                elif tmp[0].lstrip() == 'InitRound':
                    if self.ctf_gametype:
                        with players_lock:
                            for player in game.players.itervalues():
                                player.reset_flag_stats()
                    elif self.ts_gametype:
                        self.allow_cmd_teams = False
                elif tmp[0].lstrip() == 'ClientUserinfo':
                    self.handle_userinfo(line)
                elif tmp[0].lstrip() == 'ClientUserinfoChanged':
                    self.handle_userinfo_changed(line)
                elif tmp[0].lstrip() == 'ClientBegin':
                    self.handle_begin(line)
                elif tmp[0].lstrip() == 'ClientDisconnect':
                    self.handle_disconnect(line)
                elif tmp[0].lstrip() == 'Kill':
                    self.handle_kill(line)
                elif tmp[0].lstrip() == 'Hit':
                    self.handle_hit(line)
                elif tmp[0].lstrip() == 'ShutdownGame':
                    self.debug("Shutting down game...")
                    game.rcon_handle.clear()
                elif tmp[0].lstrip() == 'say':
                    self.handle_say(line)
                elif tmp[0].lstrip() == 'Flag':
                    self.handle_flag(line)
                elif tmp[0].lstrip() == 'Exit':
                    self.handle_awards()
                    self.allow_cmd_teams = True
                elif tmp[0].lstrip() == 'SurvivorWinner':
                    self.handle_teams_ts_mode()
        except (IndexError, KeyError):
            pass
        except Exception, err:
            print "%s: %s" % (err.__class__.__name__, err)

    def explode_line(self, line):
        """
        explode line
        """
        arr = line.lstrip().lstrip('\\').split('\\')
        key = True
        key_val = None
        values = {}
        for item in arr:
            if key:
                key_val = item
                key = False
            else:
                values[key_val.rstrip()] = item.rstrip()
                key_val = None
                key = True
        return values

    def handle_userinfo(self, line):
        """
        handle player user information, auto-kick known cheater ports or guids
        """
        with players_lock:
            player_num = int(line[:2].strip())
            line = line[2:].lstrip("\\").lstrip()
            values = self.explode_line(line)
            challenge = True if 'challenge' in values else False
            try:
                guid = values['cl_guid'].rstrip('\n')
                name = re.sub(r"\s+", "", values['name'])
                ip_port = values['ip']
            except KeyError:
                if 'cl_guid' in values:
                    guid = values['cl_guid']
                elif 'skill' in values:
                    # bot connecting
                    guid = "BOT%d" % player_num
                else:
                    guid = "None"
                    game.send_rcon("Player with invalid GUID kicked")
                    game.send_rcon("kick %d" % player_num)
                if 'name' in values:
                    name = re.sub(r"\s+", "", values['name'])
                else:
                    name = "UnnamedPlayer"
                    game.send_rcon("Player with invalid name kicked")
                    game.send_rcon("kick %d" % player_num)
                if 'ip' in values:
                    ip_port = values['ip']
                else:
                    ip_port = "0.0.0.0:0"

            address = ip_port.split(":")[0].strip()
            port = ip_port.split(":")[1].strip()

            # kick player with hax port 1337 or 1024
            if port == "1337" or port == "1024":
                game.send_rcon("Cheater Port detected for %s -> Player kicked" % name)
                game.send_rcon("kick %d" % player_num)
            # kick player with hax guid 'kemfew'
            if "KEMFEW" in guid.upper():
                game.send_rcon("Cheater GUID detected for %s -> Player kicked" % name)
                game.send_rcon("kick %d" % player_num)
            if "WORLD" in guid.upper() or "UNKNOWN" in guid.upper():
                game.send_rcon("Invalid GUID detected for %s -> Player kicked" % name)
                game.send_rcon("kick %d" % player_num)

            if player_num not in game.players:
                player = Player(player_num, address, guid, name)
                game.add_player(player)
            if game.players[player_num].get_guid() != guid:
                game.players[player_num].set_guid(guid)
            if game.players[player_num].get_name() != name:
                game.players[player_num].set_name(name)
            if challenge:
                self.debug("Player %d %s is challenging the server and has the guid %s" % (player_num, name, guid))
            else:
                if 'name' in values and values['name'] != game.players[player_num].get_name():
                    game.players[player_num].set_name(values['name'])

            # kick banned player
            if game.players[player_num].get_banned_player():
                game.send_rcon("kick %d" % player_num)

    def handle_userinfo_changed(self, line):
        """
        handle player changes
        """
        with players_lock:
            player_num = int(line[:2].strip())
            player = game.players[player_num]
            line = line[2:].lstrip("\\")
            try:
                values = self.explode_line(line)
                team_num = values['t']
                player.set_team(int(team_num))
                name = re.sub(r"\s+", "", values['n'])
            except KeyError:
                player.set_team(3)
                team_num = "3"
                name = "UnnamedPlayer"
            team_dict = {0: "GREEN", 1: "RED", 2: "BLUE", 3: "SPEC"}
            team = team_dict[team_num] if team_num in team_dict else "SPEC"
            if not(game.players[player_num].get_name() == name):
                game.players[player_num].set_name(name)
            self.debug("Player %d %s is on the %s team" % (player_num, name, team))

    def handle_begin(self, line):
        """
        handle player entering game
        """
        with players_lock:
            player_num = int(line[:2].strip())
            player = game.players[player_num]
            player_name = player.get_name()
            # Welcome message for registered players
            if player.get_registered_user() and player.get_welcome_msg():
                game.rcon_tell(player_num, "^7[^2Authed^7] Welcome back %s, you are ^2%s^7, last visit %s, you played %s times" % (player_name, player.roles[player.get_admin_role()], player.get_last_visit(), player.get_num_played()), False)
                # disable welcome message for next rounds
                player.disable_welcome_msg()
            self.debug("Player %d %s has entered the game" % (player_num, player_name))

    def handle_disconnect(self, line):
        """
        handle player disconnect
        """
        with players_lock:
            player_num = int(line[:2].strip())
            player = game.players[player_num]
            player.save_info()
            player.reset()
            del game.players[player_num]
            self.debug("Player %d %s has left the game" % (player_num, player.get_name()))

    def handle_hit(self, line):
        """
        handle all kind of hits
        """
        with players_lock:
            parts = line.split(":", 1)
            info = parts[0].split(" ")
            hitter_id = int(info[1])
            victim_id = int(info[0])
            hitter = game.players[hitter_id]
            victim = game.players[victim_id]
            hitter_name = hitter.get_name()
            victim_name = victim.get_name()
            hitpoint = int(info[2])
            hit_item = int(info[3])
            # increase summary of all hits
            hitter.set_all_hits()

            if hitpoint in self.hit_points:
                if self.hit_points[hitpoint] == 'HEAD' or self.hit_points[hitpoint] == 'HELMET':
                    hitter.headshot()
                    hitter_hs_count = hitter.get_headshots()
                    player_color = "^1" if (hitter.get_team() == 1) else "^4"
                    hs_plural = "headshots" if hitter_hs_count > 1 else "headshot"
                    if game.live:
                        percentage = int(round(float(hitter_hs_count) / float(hitter.get_all_hits()), 2) * 100)
                        game.send_rcon("%s%s ^7has %d %s (%d percent)" % (player_color, hitter_name, hitter_hs_count, hs_plural, percentage))
                self.debug("Player %d %s hit %d %s in the %s with %s" % (hitter_id, hitter_name, victim_id, victim_name, self.hit_points[hitpoint], self.hit_item[hit_item]))

    def handle_kill(self, line):
        """
        handle kills
        """
        with players_lock:
            parts = line.split(":", 1)
            info = parts[0].split(" ")
            k_name = parts[1].strip().split(" ")[0]
            killer_id = int(info[0])
            victim_id = int(info[1])
            death_cause = self.death_cause[int(info[2])]
            victim = game.players[victim_id]

            if k_name != "<non-client>":
                killer = game.players[killer_id]
            else:
                # killed by World
                killer = game.players[1022]
                killer_id = 1022

            killer_name = killer.get_name()
            victim_name = victim.get_name()
            tk_event = False

            # teamkill event - disabled for FFA, LMS, Jump, for all other game modes team kills are counted and punished
            if not self.ffa_lms_gametype:
                if (victim.get_team() == killer.get_team() and victim_id != killer_id) and death_cause != "UT_MOD_BOMBED":
                    # increase team kill counter for killer and kick for too many team kills
                    killer.team_kill(victim, self.tk_autokick)
                    tk_event = True
                    # increase team death counter for victim
                    victim.team_death()

            suicide_reason = ['UT_MOD_SUICIDE', 'MOD_FALLING', 'MOD_WATER', 'MOD_LAVA', 'MOD_TRIGGER_HURT', 'UT_MOD_SPLODED']
            # suicide counter
            if death_cause in suicide_reason or (killer_id == victim_id and (death_cause == 'UT_MOD_HEGRENADE' or death_cause == 'UT_MOD_HK69' or death_cause == 'UT_MOD_NUKED' or death_cause == 'UT_MOD_SLAPPED' or death_cause == 'UT_MOD_BOMBED')):
                victim.suicide()
                victim.die()
                self.debug("Player %d %s committed suicide with %s" % (victim_id, victim_name, death_cause))
            # kill counter
            elif not tk_event and int(info[2]) != 10:  # 10: MOD_CHANGE_TEAM
                killer.kill()
                killer_color = "^1" if (killer.get_team() == 1) else "^4"
                if killer.get_killing_streak() == 5 and killer_id != 1022:
                    game.rcon_say("%s%s ^7is on a killing spree!" % (killer_color, killer_name))
                elif killer.get_killing_streak() == 10 and killer_id != 1022:
                    game.rcon_say("%s%s ^7is on a rampage!" % (killer_color, killer_name))
                elif killer.get_killing_streak() == 15 and killer_id != 1022:
                    game.rcon_say("%s%s ^7is unstoppable!" % (killer_color, killer_name))
                elif killer.get_killing_streak() == 20 and killer_id != 1022:
                    game.rcon_say("%s%s ^7is godlike!" % (killer_color, killer_name))

                victim_color = "^1" if (victim.get_team() == 1) else "^4"
                if victim.get_killing_streak() >= 20 and killer_name != victim_name and killer_id != 1022:
                    game.rcon_say("%s%s's ^7godlike was ended by %s%s!" % (victim_color, victim_name, killer_color, killer_name))
                elif victim.get_killing_streak() >= 15 and killer_name != victim_name and killer_id != 1022:
                    game.rcon_say("%s%s's ^7unstoppable was ended by %s%s!" % (victim_color, victim_name, killer_color, killer_name))
                elif victim.get_killing_streak() >= 10 and killer_name != victim_name and killer_id != 1022:
                    game.rcon_say("%s%s's ^7rampage was ended by %s%s!" % (victim_color, victim_name, killer_color, killer_name))
                elif victim.get_killing_streak() >= 5 and killer_name != victim_name and killer_id != 1022:
                    game.rcon_say("%s%s's ^7killing spree was ended by %s%s!" % (victim_color, victim_name, killer_color, killer_name))
                victim.die()
                self.debug("Player %d %s killed %d %s with %s" % (killer_id, killer_name, victim_id, victim_name, death_cause))

    def player_found(self, user):
        """
        return True and instance of player or False and message text
        """
        victim = None
        name_list = []
        for player in game.players.itervalues():
            player_name = player.get_name()
            player_num = player.get_player_num()
            if (user.upper() == player_name.upper() or user == str(player_num)) and player_num != 1022:
                victim = player
                name_list = ["^3%s [^2%d^3]" % (player_name, player_num)]
                break
            elif user.upper() in player_name.upper() and player_num != 1022:
                victim = player
                name_list.append("^3%s [^2%d^3]" % (player_name, player_num))
        if len(name_list) == 0:
            return False, None, "No Player found"
        elif len(name_list) > 1:
            return False, None, "^7Players matching %s: ^3%s" % (user, ', '.join(name_list))
        else:
            return True, victim, None

    def map_found(self, map_name):
        """
        return True and map name or False and message text
        """
        map_list = []
        for maps in game.get_all_maps():
            if map_name.lower() == maps or ('ut4_%s' % map_name.lower()) == maps:
                map_list.append(maps)
                break
            elif map_name.lower() in maps:
                map_list.append(maps)
        if len(map_list) == 0:
            return False, None, "Map not found"
        elif len(map_list) > 1:
            return False, None, "^7Maps matching %s: ^3%s" % (map_name, ', '.join(map_list))
        else:
            return True, map_list[0], None

    def handle_say(self, line):
        """
        handle say commands
        """
        reason_dict = {'obj': 'go for objective', 'camp': 'stop camping', 'spam': 'do not spam, shut-up!', 'lang': 'bad language', 'racism': 'racism is not tolerated',
                       'ping': 'fix your ping', 'afk': 'away from keyboard', 'tk': 'stop team killing', 'spec': 'spectator too long on full server', 'ci': 'connection interrupted'}

        with players_lock:
            line = line.strip()
            try:
                tmp = line.split(" ")
                sar = {'player_num': int(tmp[0]), 'name': tmp[1], 'command': tmp[2]}
            except IndexError:
                sar = {'player_num': None, 'name': None, 'command': None}

            if sar['command'] == '!mapstats':
                game.rcon_tell(sar['player_num'], "^2%d ^7kills - ^2%d ^7deaths" % (game.players[sar['player_num']].get_kills(), game.players[sar['player_num']].get_deaths()))
                game.rcon_tell(sar['player_num'], "^2%d ^7kills in a row - ^2%d ^7teamkills" % (game.players[sar['player_num']].get_killing_streak(), game.players[sar['player_num']].get_team_kill_count()))
                game.rcon_tell(sar['player_num'], "^2%d ^7total hits - ^2%d ^7headshots" % (game.players[sar['player_num']].get_all_hits(), game.players[sar['player_num']].get_headshots()))
                if self.ctf_gametype:
                    game.rcon_tell(sar['player_num'], "^2%d ^7flags captured - ^2%d ^7flags returned" % (game.players[sar['player_num']].get_flags_captured(), game.players[sar['player_num']].get_flags_returned()))

            elif sar['command'] == '!help' or sar['command'] == '!h':
                ## TO DO - specific help for each command
                if game.players[sar['player_num']].get_admin_role() < 20:
                    game.rcon_tell(sar['player_num'], "^7Available commands:")
                    game.rcon_tell(sar['player_num'], ", ".join(self.user_cmds), False)
                # help for mods - additional commands
                elif game.players[sar['player_num']].get_admin_role() == 20:
                    game.rcon_tell(sar['player_num'], "^7Moderator commands:")
                    game.rcon_tell(sar['player_num'], ", ".join(self.mod_cmds), False)
                # help for admins - additional commands
                elif game.players[sar['player_num']].get_admin_role() == 40:
                    game.rcon_tell(sar['player_num'], "^7Admin commands:")
                    game.rcon_tell(sar['player_num'], ", ".join(self.admin_cmds), False)
                elif game.players[sar['player_num']].get_admin_role() == 60:
                    game.rcon_tell(sar['player_num'], "^7Full Admin commands:")
                    game.rcon_tell(sar['player_num'], ", ".join(self.fulladmin_cmds), False)
                elif game.players[sar['player_num']].get_admin_role() >= 80:
                    game.rcon_tell(sar['player_num'], "^7Senior Admin commands:")
                    game.rcon_tell(sar['player_num'], ", ".join(self.senioradmin_cmds), False)

## player commands
            # register - register yourself as a basic user
            elif sar['command'] == '!register':
                if not game.players[sar['player_num']].get_registered_user():
                    game.players[sar['player_num']].register_user_db(role=1)
                    game.rcon_tell(sar['player_num'], "%s ^7put in group User" % game.players[sar['player_num']].get_name())
                else:
                    game.rcon_tell(sar['player_num'], "%s ^7is already in a higher level group" % game.players[sar['player_num']].get_name())

            # hs - display headshot counter
            elif sar['command'] == '!hs':
                hs_count = game.players[sar['player_num']].get_headshots()
                if hs_count > 0:
                    game.rcon_tell(sar['player_num'], "^7You made ^2%d ^7headshot%s" % (hs_count, 's' if hs_count > 1 else ''))
                else:
                    game.rcon_tell(sar['player_num'], "^7You made no headshot")

            # spree - display kill streak counter
            elif sar['command'] == '!spree':
                spree_count = game.players[sar['player_num']].get_killing_streak()
                if spree_count > 0:
                    game.rcon_tell(sar['player_num'], "^7You have ^2%d ^7kill%s in a row" % (spree_count, 's' if spree_count > 1 else ''))
                else:
                    game.rcon_tell(sar['player_num'], "^7You are currently not having a killing spree")

            # time - display the servers current time
            elif sar['command'] == '!time' or sar['command'] == '@time':
                msg = "^7%s" % time.strftime("%H:%M", time.localtime(time.time()))
                self.tell_say_message(sar, msg)

            # teams - balance teams
            elif sar['command'] == '!teams':
                if not self.ffa_lms_gametype:
                    self.handle_team_balance()

            # stats - display current map stats
            elif sar['command'] == '!stats':
                if game.players[sar['player_num']].get_deaths() == 0:
                    ratio = 1.0
                else:
                    ratio = round(float(game.players[sar['player_num']].get_kills()) / float(game.players[sar['player_num']].get_deaths()), 2)
                game.rcon_tell(sar['player_num'], "^7Map Stats %s: ^7K ^2%d ^7D ^3%d ^7TK ^1%d ^7Ratio ^5%s ^7HS ^2%d" % (game.players[sar['player_num']].get_name(), game.players[sar['player_num']].get_kills(), game.players[sar['player_num']].get_deaths(), game.players[sar['player_num']].get_team_kill_count(), ratio, game.players[sar['player_num']].get_headshots()))

            # xlrstats - display full player stats
            elif sar['command'] == '!xlrstats':
                if line.split(sar['command'])[1]:
                    arg = line.split(sar['command'])[1].strip()
                    for player in game.players.itervalues():
                        if (arg.upper() in (player.get_name()).upper()) or arg == str(player.get_player_num()):
                            if player.get_registered_user():
                                if player.get_db_deaths() == 0:
                                    ratio = 1.0
                                else:
                                    ratio = round(float(player.get_db_kills()) / float(player.get_db_deaths()), 2)
                                game.rcon_tell(sar['player_num'], "^7Stats %s: ^7K ^2%d ^7D ^3%d ^7TK ^1%d ^7Ratio ^5%s ^7HS ^2%d" % (player.get_name(), player.get_db_kills(), player.get_db_deaths(), player.get_db_tks(), ratio, player.get_db_headshots()))
                            else:
                                game.rcon_tell(sar['player_num'], "^7Sorry, this player is not registered")
                else:
                    if game.players[sar['player_num']].get_registered_user():
                        if game.players[sar['player_num']].get_db_deaths() == 0:
                            ratio = 1.0
                        else:
                            ratio = round(float(game.players[sar['player_num']].get_db_kills()) / float(game.players[sar['player_num']].get_db_deaths()), 2)
                        game.rcon_tell(sar['player_num'], "^7Stats %s: ^7K ^2%d ^7D ^3%d ^7TK ^1%d ^7Ratio ^5%s ^7HS ^2%d" % (game.players[sar['player_num']].get_name(), game.players[sar['player_num']].get_db_kills(), game.players[sar['player_num']].get_db_deaths(), game.players[sar['player_num']].get_db_tks(), ratio, game.players[sar['player_num']].get_db_headshots()))
                    else:
                        game.rcon_tell(sar['player_num'], "^7You need to ^2!register ^7first")

            # forgive last team kill
            elif sar['command'] == '!forgiveprev' or sar['command'] == '!fp' or sar['command'] == '!f':
                victim = game.players[sar['player_num']]
                if victim.get_killed_me():
                    forgive_player_num = victim.get_killed_me()[-1]
                    forgive_player = game.players[forgive_player_num]
                    victim.clear_tk(forgive_player_num)
                    forgive_player.clear_killed_me(victim.get_player_num())
                    game.rcon_say("^7%s has forgiven %s's attack" % (victim.get_name(), forgive_player.get_name()))
                else:
                    game.rcon_tell(sar['player_num'], "No one to forgive")

            # forgive all team kills
            elif sar['command'] == '!forgiveall' or sar['command'] == '!fa':
                victim = game.players[sar['player_num']]
                msg = []
                if victim.get_killed_me():
                    all_forgive_player_num_list = victim.get_killed_me()
                    forgive_player_num_list = list(set(all_forgive_player_num_list))
                    victim.clear_all_tk()
                    for forgive_player_num in forgive_player_num_list:
                        forgive_player = game.players[forgive_player_num]
                        forgive_player.clear_killed_me(victim.get_player_num())
                        msg.append(forgive_player.get_name())
                if msg:
                    game.rcon_say("^7%s has forgiven: %s" % (victim.get_name(), ", ".join(msg)))
                else:
                    game.rcon_tell(sar['player_num'], "No one to forgive")

## mod level 20
            # country
            elif (sar['command'] == '!country' or sar['command'] == '@country') and game.players[sar['player_num']].get_admin_role() >= 20:
                if line.split(sar['command'])[1]:
                    user = line.split(sar['command'])[1].strip()
                    found, victim, msg = self.player_found(user)
                    if not found:
                        game.rcon_tell(sar['player_num'], msg)
                    else:
                        msg = "Country ^3%s: ^7%s" % (victim.get_name(), victim.get_country())
                        self.tell_say_message(sar, msg)
                else:
                    game.rcon_tell(sar['player_num'], "^7Usage: !country <name>")

            # leveltest
            elif (sar['command'] == '!leveltest' or sar['command'] == '!lt') and game.players[sar['player_num']].get_admin_role() >= 20:
                if line.split(sar['command'])[1]:
                    user = line.split(sar['command'])[1].strip()
                    found, victim, msg = self.player_found(user)
                    if not found:
                        game.rcon_tell(sar['player_num'], msg)
                    else:
                        game.rcon_tell(sar['player_num'], "Level ^3%s [^2%d^3]: ^7%s" % (victim.get_name(), victim.get_admin_role(), victim.roles[victim.get_admin_role()]))
                else:
                    game.rcon_tell(sar['player_num'], "Level ^3%s [^2%d^3]: ^7%s" % (game.players[sar['player_num']].get_name(), game.players[sar['player_num']].get_admin_role(), game.players[sar['player_num']].roles[game.players[sar['player_num']].get_admin_role()]))

            # list - list all connected players
            elif sar['command'] == '!list' and game.players[sar['player_num']].get_admin_role() >= 20:
                msg = "^7Players online: %s" % ", ".join(["^3%s [^2%d^3]" % (player.get_name(), player.get_player_num()) for player in game.players.itervalues() if player.get_player_num() != 1022])
                game.rcon_tell(sar['player_num'], msg)

            # nextmap - display the next map in rotation
            elif (sar['command'] == '!nextmap' or sar['command'] == '@nextmap') and game.players[sar['player_num']].get_admin_role() >= 20:
                g_nextmap = game.rcon_handle.get_cvar('g_nextmap').split(" ")[0].strip()
                if g_nextmap in game.get_all_maps():
                    msg = "^7Next Map: ^3%s" % g_nextmap
                    game.next_mapname = g_nextmap
                else:
                    msg = "^7Next Map: ^3%s" % game.next_mapname
                self.tell_say_message(sar, msg)

            # mute - mute or unmute a player
            elif sar['command'] == '!mute' and game.players[sar['player_num']].get_admin_role() >= 20:
                if line.split(sar['command'])[1]:
                    arg = line.split(sar['command'])[1].strip().split(' ')
                    if len(arg) > 1:
                        user = arg[0]
                        duration = arg[1]
                        if not duration.isdigit():
                            duration = ''
                    else:
                        user = arg[0]
                        duration = ''
                    found, victim, msg = self.player_found(user)
                    if not found:
                        game.rcon_tell(sar['player_num'], msg)
                    else:
                        game.send_rcon("mute %d %s" % (victim.get_player_num(), duration))
                else:
                    game.rcon_tell(sar['player_num'], "^7Usage: !mute <name> [<seconds>]")

            # shuffleteams
            elif (sar['command'] == '!shuffleteams' or sar['command'] == '!shuffle') and game.players[sar['player_num']].get_admin_role() >= 20:
                if not self.ffa_lms_gametype:
                    game.send_rcon('shuffleteams')
                else:
                    game.rcon_tell(sar['player_num'], "^7Command is disabled for this game mode")

            # warn - warn user
            elif (sar['command'] == '!warn' or sar['command'] == '!w') and game.players[sar['player_num']].get_admin_role() >= 20:
                if line.split(sar['command'])[1]:
                    arg = line.split(sar['command'])[1].strip().split(' ')
                    if len(arg) > 1:
                        user = arg[0]
                        reason = ' '.join(arg[1:])
                        found, victim, msg = self.player_found(user)
                        if not found:
                            game.rcon_tell(sar['player_num'], msg)
                        else:
                            if victim.get_admin_role() >= game.players[sar['player_num']].get_admin_role():
                                game.rcon_tell(sar['player_num'], "You cannot warn an admin")
                            else:
                                if victim.get_warning() > 2:
                                    game.kick_player(victim.get_player_num())
                                    msg = "^7Player ^2%s ^7kicked, because of too many warnings" % victim.get_name()
                                else:
                                    victim.add_warning()
                                    msg = "^1WARNING ^7[^3%d^7]: ^2%s^7: " % (victim.get_warning(), victim.get_name())
                                    if reason in reason_dict:
                                        msg = "%s%s" % (msg, reason_dict[reason])
                                        if reason == 'tk' and victim.get_warning() > 1:
                                            victim.add_ban_point('tk, ban by %s' % game.players[sar['player_num']].get_name(), 600)
                                        elif reason == 'lang' and victim.get_warning() > 1:
                                            victim.add_ban_point('lang', 300)
                                        elif reason == 'spam' and victim.get_warning() > 1:
                                            victim.add_ban_point('spam', 300)
                                        elif reason == 'racism' and victim.get_warning() > 1:
                                            victim.add_ban_point('racism', 300)
                                    else:
                                        msg = "%s%s" % (msg, reason)
                                game.rcon_say(msg)
                    else:
                        game.rcon_tell(sar['player_num'], "^7You need to enter a reason: ^3!warn <name> <reason>")
                else:
                    game.rcon_tell(sar['player_num'], "^7Usage: !warn <name> <reason>")

## admin level 40
            # admins - list all the online admins
            elif (sar['command'] == '!admins' or sar['command'] == '@admins') and game.players[sar['player_num']].get_admin_role() >= 40:
                msg = "^7Admins online: %s" % ", ".join(["^3%s [^2%d^3]" % (player.get_name(), player.get_admin_role()) for player in game.players.itervalues() if player.get_admin_role() >= 20])
                self.tell_say_message(sar, msg)

            # aliases - list the aliases of the player
            elif (sar['command'] == '!aliases' or sar['command'] == '@aliases' or sar['command'] == '!alias' or sar['command'] == '@alias') and game.players[sar['player_num']].get_admin_role() >= 40:
                if line.split(sar['command'])[1]:
                    user = line.split(sar['command'])[1].strip()
                    found, victim, msg = self.player_found(user)
                    if not found:
                        game.rcon_tell(sar['player_num'], msg)
                    else:
                        msg = "^7Aliases of ^5%s: ^3%s" % (victim.get_name(), victim.get_aliases())
                        self.tell_say_message(sar, msg)
                else:
                    game.rcon_tell(sar['player_num'], "^7Usage: !alias <name>")

            # bigtext - display big message on screen
            elif sar['command'] == '!bigtext' and game.players[sar['player_num']].get_admin_role() >= 40:
                if line.split(sar['command'])[1]:
                    game.rcon_bigtext("%s" % line.split(sar['command'])[1].strip())
                else:
                    game.rcon_tell(sar['player_num'], "^7Usage: !bigtext <text>")

            # say - say a message to all players
            elif sar['command'] == '!say' and game.players[sar['player_num']].get_admin_role() >= 40:
                if line.split(sar['command'])[1]:
                    game.rcon_say("%s" % line.split(sar['command'])[1].strip())
                else:
                    game.rcon_tell(sar['player_num'], "^7Usage: !say <text>")

            # force - force a player to the given team
            elif sar['command'] == '!force' and game.players[sar['player_num']].get_admin_role() >= 40:
                if line.split(sar['command'])[1]:
                    arg = line.split(sar['command'])[1].strip().split(' ')
                    if len(arg) > 1:
                        user = arg[0]
                        team = arg[1]
                        team_dict = {'red': 'red', 'r': 'red', 're': 'red',
                                     'blue': 'blue', 'b': 'blue', 'bl': 'blue', 'blu': 'blue',
                                     'spec': 'spectator', 'spectator': 'spectator', 's': 'spectator', 'sp': 'spectator', 'spe': 'spectator',
                                     'green': 'green'}
                        found, victim, msg = self.player_found(user)
                        if not found:
                            game.rcon_tell(sar['player_num'], msg)
                        else:
                            if team in team_dict:
                                victim_player_num = victim.get_player_num()
                                game.rcon_forceteam(victim_player_num, team_dict[team])
                                game.rcon_tell(victim_player_num, "^3You are forced to: ^7%s" % team_dict[team])
                            else:
                                game.rcon_tell(sar['player_num'], "^7Usage: !force <name> <blue/red/spec>")
                    else:
                        game.rcon_tell(sar['player_num'], "^7Usage: !force <name> <blue/red/spec>")
                else:
                    game.rcon_tell(sar['player_num'], "^7Usage: !force <name> <blue/red/spec>")

            # nuke - nuke a player
            elif sar['command'] == '!nuke' and game.players[sar['player_num']].get_admin_role() >= 40:
                if line.split(sar['command'])[1]:
                    user = line.split(sar['command'])[1].strip()
                    found, victim, msg = self.player_found(user)
                    if not found:
                        game.rcon_tell(sar['player_num'], msg)
                    else:
                        if victim.get_admin_role() >= game.players[sar['player_num']].get_admin_role():
                            game.rcon_tell(sar['player_num'], "You cannot nuke an admin")
                        else:
                            game.send_rcon("nuke %d" % victim.get_player_num())
                else:
                    game.rcon_tell(sar['player_num'], "^7Usage: !nuke <name>")

            # kick - kick a player
            elif (sar['command'] == '!kick' or sar['command'] == '!k') and game.players[sar['player_num']].get_admin_role() >= 40:
                if line.split(sar['command'])[1]:
                    arg = line.split(sar['command'])[1].strip().split(' ')
                    if len(arg) > 1:
                        user = arg[0]
                        reason = ' '.join(arg[1:])
                        found, victim, msg = self.player_found(user)
                        if not found:
                            game.rcon_tell(sar['player_num'], msg)
                        else:
                            if victim.get_admin_role() >= game.players[sar['player_num']].get_admin_role():
                                game.rcon_tell(sar['player_num'], "You cannot kick an admin")
                            else:
                                game.kick_player(victim.get_player_num())
                                msg = "^2%s ^7was kicked by %s: ^4" % (victim.get_name(), game.players[sar['player_num']].get_name())
                                if reason in reason_dict:
                                    msg = "%s%s" % (msg, reason_dict[reason])
                                else:
                                    msg = "%s%s" % (msg, reason)
                                game.rcon_say(msg)
                    else:
                        game.rcon_tell(sar['player_num'], "^7You need to enter a reason: ^3!kick <name> <reason>")
                else:
                    game.rcon_tell(sar['player_num'], "^7Usage: !kick <name> <reason>")

            # warnclear - clear the user warnings
            elif (sar['command'] == '!warnclear' or sar['command'] == '!wc' or sar['command'] == '!wr') and game.players[sar['player_num']].get_admin_role() >= 40:
                if line.split(sar['command'])[1]:
                    user = line.split(sar['command'])[1].strip()
                    found, victim, msg = self.player_found(user)
                    if not found:
                        game.rcon_tell(sar['player_num'], msg)
                    else:
                        victim.clear_warning()
                        game.rcon_say("^1All warnings cleared for ^2%s" % victim.get_name())
                else:
                    game.rcon_tell(sar['player_num'], "^7Usage: !warnclear <name>")

            # tempban - ban a player temporary for given period in hours (1-24 hrs)
            elif (sar['command'] == '!tempban' or sar['command'] == '!tb') and game.players[sar['player_num']].get_admin_role() >= 40:
                if line.split(sar['command'])[1]:
                    arg = line.split(sar['command'])[1].strip().split(' ')
                    if len(arg) > 1:
                        user = arg[0]
                        if len(arg) == 2:
                            reason = arg[1]
                            duration_string = '1'
                        else:
                            reason = arg[1]
                            duration_string = arg[2].rstrip('hm')
                        if reason.rstrip('hm').isdigit():
                            game.rcon_tell(sar['player_num'], "^7You need to enter a reason: ^3!tempban <name> <reason> [<duration in hours>]")
                        else:
                            if duration_string.isdigit():
                                duration = int(duration_string) * 3600
                            else:
                                duration = 3600
                            if duration == 3600:
                                duration_output = "1 hour"
                            else:
                                duration_output = "%s hours" % duration_string
                            if duration > 86400:
                                duration = 86400
                                duration_output = "24 hours"
                            found, victim, msg = self.player_found(user)
                            if not found:
                                game.rcon_tell(sar['player_num'], msg)
                            else:
                                if victim.get_admin_role() >= game.players[sar['player_num']].get_admin_role():
                                    game.rcon_tell(sar['player_num'], "You cannot ban an admin")
                                else:
                                    victim.ban(duration=duration, reason=reason, admin=game.players[sar['player_num']].get_name())
                                    game.rcon_say("^2%s ^1banned for %s ^7by %s: ^4%s" % (victim.get_name(), duration_output, game.players[sar['player_num']].get_name(), reason))
                    else:
                        game.rcon_tell(sar['player_num'], "^7You need to enter a reason: ^3!tempban <name> <reason> [<duration in hours>]")
                else:
                    game.rcon_tell(sar['player_num'], "^7Usage: !tempban <name> <reason> [<duration in hours>]")

## full admin level 60
            # scream - scream a message in different colors to all players
            elif sar['command'] == '!scream' and game.players[sar['player_num']].get_admin_role() >= 60:
                if line.split(sar['command'])[1]:
                    game.rcon_say("^1%s" % line.split(sar['command'])[1].strip())
                    game.rcon_say("^2%s" % line.split(sar['command'])[1].strip())
                    game.rcon_say("^3%s" % line.split(sar['command'])[1].strip())
                    game.rcon_say("^5%s" % line.split(sar['command'])[1].strip())
                else:
                    game.rcon_tell(sar['player_num'], "^7Usage: !scream <text>")

            # slap - slap a player (a number of times); (1-10 times)
            elif (sar['command'] == '!slap' or sar['command'] == '!spank') and game.players[sar['player_num']].get_admin_role() >= 60:
                if line.split(sar['command'])[1]:
                    arg = line.split(sar['command'])[1].strip().split(' ')
                    if len(arg) > 1:
                        user = arg[0]
                        number = arg[1]
                        if not number.isdigit():
                            number = 1
                        else:
                            number = int(number)
                        if number > 10:
                            number = 10
                    else:
                        user = arg[0]
                        number = 1
                    found, victim, msg = self.player_found(user)
                    if not found:
                        game.rcon_tell(sar['player_num'], msg)
                    else:
                        if victim.get_admin_role() >= game.players[sar['player_num']].get_admin_role():
                            game.rcon_tell(sar['player_num'], "You cannot slap an admin")
                        else:
                            for _ in xrange(0, number):
                                game.send_rcon("slap %d" % victim.get_player_num())
                else:
                    game.rcon_tell(sar['player_num'], "^7Usage: !slap <name> [<amount>]")

            # swap - swap teams for player 1 and 2 (if in different teams)
            elif sar['command'] == '!swap' and game.players[sar['player_num']].get_admin_role() >= 60:
                if not self.ffa_lms_gametype:
                    if line.split(sar['command'])[1]:
                        arg = line.split(sar['command'])[1].strip().split(' ')
                        if len(arg) > 1:
                            player1 = arg[0]
                            player2 = arg[1]
                            found1, victim1, _ = self.player_found(player1)
                            found2, victim2, _ = self.player_found(player2)
                            if not found1 or not found2:
                                game.rcon_tell(sar['player_num'], 'Player not found')
                            else:
                                team1 = victim1.get_team()
                                team2 = victim2.get_team()
                                if team1 == team2:
                                    game.rcon_tell(sar['player_num'], "^7Cannot swap, both players are in the same team")
                                else:
                                    game_data = game.get_gamestats()
                                    if game_data[Player.teams[team1]] < game_data[Player.teams[team2]]:
                                        game.rcon_forceteam(victim2.get_player_num(), Player.teams[team1])
                                        game.rcon_forceteam(victim1.get_player_num(), Player.teams[team2])
                                    else:
                                        game.rcon_forceteam(victim1.get_player_num(), Player.teams[team2])
                                        game.rcon_forceteam(victim2.get_player_num(), Player.teams[team1])
                                    game.rcon_say('^7Swapped player ^3%s ^7with ^3%s' % (victim1.get_name(), victim2.get_name()))
                        else:
                            game.rcon_tell(sar['player_num'], "^7Usage: !swap <name1> <name2>")
                    else:
                        game.rcon_tell(sar['player_num'], "^7Usage: !swap <name1> <name2>")
                else:
                    game.rcon_tell(sar['player_num'], "^7Command is disabled for this game mode")

            # version - display the version of the bot
            elif sar['command'] == '!version' and game.players[sar['player_num']].get_admin_role() >= 60:
                game.rcon_tell(sar['player_num'], "^7Spunky Bot ^2v%s" % __version__)
                try:
                    get_latest = urllib2.urlopen('%s/version.txt' % self.base_url).read().strip()
                except urllib2.URLError:
                    get_latest = __version__
                if __version__ < get_latest:
                    game.rcon_tell(sar['player_num'], "^7A newer release ^6%s ^7is available, check ^3www.spunkybot.de" % get_latest)

            # veto - stop voting process
            elif sar['command'] == '!veto' and game.players[sar['player_num']].get_admin_role() >= 60:
                game.send_rcon('veto')

            # ci - kick player with connection interrupted
            elif sar['command'] == '!ci' and game.players[sar['player_num']].get_admin_role() >= 60:
                if line.split(sar['command'])[1]:
                    user = line.split(sar['command'])[1].strip()
                    player_ping = 0
                    found, victim, msg = self.player_found(user)
                    if not found:
                        game.rcon_tell(sar['player_num'], msg)
                    else:
                        # update rcon status
                        game.rcon_handle.quake.rcon_update()
                        for player in game.rcon_handle.quake.players:
                            if victim.get_player_num() == player.num:
                                player_ping = player.ping
                        if player_ping == 999:
                            game.kick_player(victim.get_player_num())
                            game.rcon_say("^1%s ^7was kicked by %s: ^4connection interrupted" % (victim.get_name(), game.players[sar['player_num']].get_name()))
                        else:
                            game.rcon_tell(sar['player_num'], "%s has no connection interrupted" % victim.get_name())
                else:
                    game.rcon_tell(sar['player_num'], "^7Usage: !ci <name>")

            # ban - ban a player for 7 days
            elif (sar['command'] == '!ban' or sar['command'] == '!b') and game.players[sar['player_num']].get_admin_role() >= 60:
                if line.split(sar['command'])[1]:
                    arg = line.split(sar['command'])[1].strip().split(' ')
                    if len(arg) > 1:
                        user = arg[0]
                        reason = ' '.join(arg[1:])
                        found, victim, msg = self.player_found(user)
                        if not found:
                            game.rcon_tell(sar['player_num'], msg)
                        else:
                            if victim.get_admin_role() >= game.players[sar['player_num']].get_admin_role():
                                game.rcon_tell(sar['player_num'], "You cannot ban an admin")
                            else:
                                # ban for 7 days
                                victim.ban(duration=604800, reason=reason, admin=game.players[sar['player_num']].get_name())
                                game.rcon_say("^2%s ^1banned for 7 days ^7by %s: ^4%s" % (victim.get_name(), game.players[sar['player_num']].get_name(), reason))
                    else:
                        game.rcon_tell(sar['player_num'], "^7You need to enter a reason: ^3!ban <name> <reason>")
                else:
                    game.rcon_tell(sar['player_num'], "^7Usage: !ban <name> <reason>")

## senior admin level 80
            # kiss - clear all player warnings
            elif (sar['command'] == '!kiss' or sar['command'] == '!clear') and game.players[sar['player_num']].get_admin_role() >= 80:
                for player in game.players.itervalues():
                    player.clear_warning()
                game.rcon_say("^1All player warnings cleared")

            # map - load given map
            elif sar['command'] == '!map' and game.players[sar['player_num']].get_admin_role() >= 80:
                if line.split(sar['command'])[1]:
                    arg = line.split(sar['command'])[1].strip()
                    found, newmap, msg = self.map_found(arg)
                    if not found:
                        game.rcon_tell(sar['player_num'], msg)
                    else:
                        game.send_rcon('g_nextmap %s' % newmap)
                        game.next_mapname = newmap
                        game.rcon_tell(sar['player_num'], "^7Changing Map to: ^3%s" % newmap)
                        game.send_rcon('cyclemap')
                else:
                    game.rcon_tell(sar['player_num'], "^7Usage: !map <ut4_name>")

            # maps - display all available maps
            elif sar['command'] == '!maps' and game.players[sar['player_num']].get_admin_role() >= 80:
                game.rcon_tell(sar['player_num'], "^7Available Maps: ^3%s" % ', '.join(game.get_all_maps()))

            # maprestart - restart the map
            elif sar['command'] == '!maprestart' and game.players[sar['player_num']].get_admin_role() >= 80:
                game.send_rcon('restart')
                for player in game.players.itervalues():
                    # reset player statistics
                    player.reset()

            # moon - activate Moon mode (low gravity)
            elif sar['command'] == '!moon' and game.players[sar['player_num']].get_admin_role() >= 80:
                if line.split(sar['command'])[1]:
                    arg = line.split(sar['command'])[1].strip()
                    if arg == "off":
                        game.send_rcon('g_gravity 800')
                        game.rcon_tell(sar['player_num'], "^7Moon mode: ^1Off")
                    elif arg == "on":
                        game.send_rcon('g_gravity 100')
                        game.rcon_tell(sar['player_num'], "^7Moon mode: ^2On")
                    else:
                        game.rcon_tell(sar['player_num'], "^7Usage: !moon <on/off>")
                else:
                    game.rcon_tell(sar['player_num'], "^7Usage: !moon <on/off>")

            # cyclemap - start next map in rotation
            elif sar['command'] == '!cyclemap' and game.players[sar['player_num']].get_admin_role() >= 80:
                game.send_rcon('cyclemap')

            # setnextmap - set the given map as nextmap
            elif sar['command'] == '!setnextmap' and game.players[sar['player_num']].get_admin_role() >= 80:
                if line.split(sar['command'])[1]:
                    arg = line.split(sar['command'])[1].strip()
                    found, nextmap, msg = self.map_found(arg)
                    if not found:
                        game.rcon_tell(sar['player_num'], msg)
                    else:
                        game.send_rcon('g_nextmap %s' % nextmap)
                        game.next_mapname = nextmap
                        game.rcon_tell(sar['player_num'], "^7Next Map set to: ^3%s" % nextmap)
                else:
                    game.rcon_tell(sar['player_num'], "^7Usage: !setnextmap <ut4_name>")

            # kill - kill a player
            elif sar['command'] == '!kill' and self.urt42_modversion and game.players[sar['player_num']].get_admin_role() >= 80:
                if line.split(sar['command'])[1]:
                    user = line.split(sar['command'])[1].strip()
                    found, victim, msg = self.player_found(user)
                    if not found:
                        game.rcon_tell(sar['player_num'], msg)
                    else:
                        if victim.get_admin_role() >= game.players[sar['player_num']].get_admin_role():
                            game.rcon_tell(sar['player_num'], "You cannot kill an admin")
                        else:
                            game.send_rcon("smite %d" % victim.get_player_num())
                else:
                    game.rcon_tell(sar['player_num'], "^7Usage: !kill <name>")

            # permban - ban a player permanent
            elif (sar['command'] == '!permban' or sar['command'] == '!pb') and game.players[sar['player_num']].get_admin_role() >= 80:
                if line.split(sar['command'])[1]:
                    arg = line.split(sar['command'])[1].strip().split(' ')
                    if len(arg) > 1:
                        user = arg[0]
                        reason = ' '.join(arg[1:])
                        found, victim, msg = self.player_found(user)
                        if not found:
                            game.rcon_tell(sar['player_num'], msg)
                        else:
                            if victim.get_admin_role() >= game.players[sar['player_num']].get_admin_role():
                                game.rcon_tell(sar['player_num'], "You cannot ban an admin")
                            else:
                                # ban for 20 years
                                victim.ban(duration=630720000, reason=reason, admin=game.players[sar['player_num']].get_name())
                                game.rcon_say("^2%s ^1banned permanently ^7by %s: ^4%s" % (victim.get_name(), game.players[sar['player_num']].get_name(), reason))
                                # add IP address to bot-banlist.txt
                                banlist = open('./bot-banlist.txt', 'a+')
                                banlist.write("%s:-1   // %s    banned on  %s, reason : %s\n" % (victim.get_ip_address(), victim.get_name(), time.strftime("%d/%m/%Y (%H:%M)", time.localtime(time.time())), reason))
                                banlist.close()
                    else:
                        game.rcon_tell(sar['player_num'], "^7You need to enter a reason: ^3!permban <name> <reason>")
                else:
                    game.rcon_tell(sar['player_num'], "^7Usage: !permban <name> <reason>")

            # putgroup - add a client to a group
            elif sar['command'] == '!putgroup' and game.players[sar['player_num']].get_admin_role() >= 80:
                if line.split(sar['command'])[1]:
                    arg = line.split(sar['command'])[1].strip().split(' ')
                    if len(arg) > 1:
                        user = arg[0]
                        right = arg[1]
                        found, victim, msg = self.player_found(user)
                        if not found:
                            game.rcon_tell(sar['player_num'], msg)
                        else:
                            if victim.get_registered_user():
                                new_role = victim.get_admin_role()
                            else:
                                # register new user in DB and set role to 1
                                victim.register_user_db(role=1)
                                new_role = 1

                            if right == "user" and victim.get_admin_role() < 80:
                                game.rcon_tell(sar['player_num'], "%s put in group User" % victim.get_name())
                                new_role = 1
                            elif right == "regular" and victim.get_admin_role() < 80:
                                game.rcon_tell(sar['player_num'], "%s put in group Regular" % victim.get_name())
                                new_role = 2
                            elif (right == "mod" or right == "moderator") and victim.get_admin_role() < 80:
                                game.rcon_tell(sar['player_num'], "%s added as Moderator" % victim.get_name())
                                new_role = 20
                            elif right == "admin" and victim.get_admin_role() < 80:
                                game.rcon_tell(sar['player_num'], "%s added as Admin" % victim.get_name())
                                new_role = 40
                            elif right == "fulladmin" and victim.get_admin_role() < 80:
                                game.rcon_tell(sar['player_num'], "%s added as Full Admin" % victim.get_name())
                                new_role = 60
                            # Note: senioradmin level can only be set by head admin
                            elif right == "senioradmin" and game.players[sar['player_num']].get_admin_role() == 100 and victim.get_player_num() != sar['player_num']:
                                game.rcon_tell(sar['player_num'], "%s added as ^6Senior Admin" % victim.get_name())
                                new_role = 80
                            else:
                                game.rcon_tell(sar['player_num'], "Sorry, you cannot put %s in group <%s>" % (victim.get_name(), right))
                            self.update_db_admin_role(victim, new_role)
                    else:
                        game.rcon_tell(sar['player_num'], "^7Usage: !putgroup <name> <group>")
                else:
                    game.rcon_tell(sar['player_num'], "^7Usage: !putgroup <name> <group>")

            # banlist - display the last 10 entries of the banlist
            elif sar['command'] == '!banlist' and game.players[sar['player_num']].get_admin_role() >= 80:
                curs.execute("SELECT * FROM `ban_list` ORDER BY `id` DESC LIMIT 10")
                result = curs.fetchall()
                if len(result) > 10:
                    limit = 10
                elif len(result) == 0:
                    limit = 0
                else:
                    limit = len(result)
                banlist = ['^7[^2%s^7]%s' % (result[item][0], result[item][2]) for item in xrange(limit)]  # 0=ID,2=Name
                msg = 'Currently no one is banned' if not banlist else str(", ".join(banlist))
                game.rcon_tell(sar['player_num'], "^7Banlist: %s" % msg)

            # unban - unban a player from the database via ID
            elif sar['command'] == '!unban' and game.players[sar['player_num']].get_admin_role() >= 80:
                if line.split(sar['command'])[1]:
                    arg = line.split(sar['command'])[1].strip()
                    if arg.isdigit():
                        values = (int(arg),)
                        curs.execute("SELECT COUNT(*) FROM `ban_list` WHERE `id` = ?", values)
                        if curs.fetchone()[0] > 0:
                            curs.execute("SELECT `name` FROM `ban_list` WHERE `id` = ?", values)
                            result = curs.fetchone()
                            name = str(result[0])
                            curs.execute("DELETE FROM `ban_list` WHERE `id` = ?", values)
                            conn.commit()
                            game.rcon_tell(sar['player_num'], "^7Player ^2%s ^7unbanned" % name)
                        else:
                            game.rcon_tell(sar['player_num'], "^7Invalid ID, no Player found")
                    else:
                        game.rcon_tell(sar['player_num'], "^7Usage: !unban <ID>")
                else:
                    game.rcon_tell(sar['player_num'], "^7Usage: !unban <ID>")

## head admin level 100
            # ungroup - remove the admin level from a player
            elif sar['command'] == '!ungroup' and game.players[sar['player_num']].get_admin_role() == 100:
                if line.split(sar['command'])[1]:
                    user = line.split(sar['command'])[1].strip()
                    found, victim, msg = self.player_found(user)
                    if not found:
                        game.rcon_tell(sar['player_num'], msg)
                    else:
                        if 1 < victim.get_admin_role() < 100:
                            game.rcon_tell(sar['player_num'], "%s put in group User" % victim.get_name())
                            self.update_db_admin_role(player=victim, role=1)
                        else:
                            game.rcon_tell(sar['player_num'], "Sorry, you cannot put %s in group User" % victim.get_name())
                else:
                    game.rcon_tell(sar['player_num'], "^7Usage: !ungroup <name>")

## iamgod
            # iamgod - register user as Head Admin
            elif sar['command'] == '!iamgod':
                if self.iamgod:
                    if not game.players[sar['player_num']].get_registered_user():
                        # register new user in DB and set admin role to 100
                        game.players[sar['player_num']].register_user_db(role=100)
                    else:
                        self.update_db_admin_role(player=game.players[sar['player_num']], role=100)
                    self.iamgod = False
                    game.rcon_tell(sar['player_num'], "^7You are registered as ^6Head Admin")

## unknown command
            elif sar['command'].startswith('!') and game.players[sar['player_num']].get_admin_role() > 20:
                game.rcon_tell(sar['player_num'], "^7Unknown command ^3%s" % sar['command'])

    def tell_say_message(self, sar, msg):
        """
        display message in private or global chat
        """
        if sar['command'].startswith('@'):
            game.rcon_say(msg)
        else:
            game.rcon_tell(sar['player_num'], msg)

    def update_db_admin_role(self, player, role):
        """
        update database and set admin_role
        """
        values = (role, player.get_guid())
        curs.execute("UPDATE `xlrstats` SET `admin_role` = ? WHERE `guid` = ?", values)
        conn.commit()
        # overwrite admin role in game, no reconnect of player required
        player.set_admin_role(role)

    def handle_flag(self, line):
        """
        handle flag
        """
        tmp = line.split(" ")
        player_num = int(tmp[0].strip())
        action = tmp[1].strip()
        with players_lock:
            player = game.players[player_num]
            if action == '1:':
                player.return_flag()
            elif action == '2:':
                player.capture_flag()

    def handle_teams_ts_mode(self):
        """
        handle team balance in Team Survivor mode
        """
        if self.ts_gametype:
            if self.ts_do_team_balance:
                self.allow_cmd_teams = True
                self.handle_team_balance()
                self.allow_cmd_teams = False
                self.ts_do_team_balance = False

    def handle_team_balance(self):
        """
        balance teams if needed
        """
        game_data = game.get_gamestats()
        if (abs(game_data[Player.teams[1]] - game_data[Player.teams[2]])) > 1:
            if self.allow_cmd_teams:
                game.balance_teams(game_data)
            else:
                if self.ts_gametype:
                    self.ts_do_team_balance = True
                    game.rcon_say("^7Teams will be balanced at the end of the round!")
        else:
            game.rcon_say("^7Teams are already balanced")

    def handle_awards(self):
        """
        display awards and personal stats at the end of the round
        """
        most_kills = 0
        most_flags = 0
        most_streak = 0
        most_hs = 0
        flagrunner = ""
        serialkiller = ""
        streaker = ""
        headshooter = ""
        msg = []
        with players_lock:
            for player in game.players.itervalues():
                if player.get_flags_captured() > most_flags:
                    most_flags = player.get_flags_captured()
                    flagrunner = player.get_name()
                if player.get_kills() > most_kills and player.get_player_num() != 1022:
                    most_kills = player.get_kills()
                    serialkiller = player.get_name()
                if player.get_max_kill_streak() > most_streak and player.get_player_num() != 1022:
                    most_streak = player.get_max_kill_streak()
                    streaker = player.get_name()
                if player.get_headshots() > most_hs:
                    most_hs = player.get_headshots()
                    headshooter = player.get_name()
                # display personal stats at the end of the round, stats for players in spec will not be displayed
                if player.get_team() != 3:
                    game.rcon_tell(player.get_player_num(), "^7Stats %s: ^7K ^2%d ^7D ^3%d ^7HS ^1%d ^7TK ^1%d" % (player.get_name(), player.get_kills(), player.get_deaths(), player.get_headshots(), player.get_team_kill_count()))
                # store score in database
                player.save_info()

            # display Awards
            if most_flags > 1:
                msg.append("^7%s: ^2%d ^4caps" % (flagrunner, most_flags))
            if most_kills > 1:
                msg.append("^7%s: ^2%d ^3kills" % (serialkiller, most_kills))
            if most_streak > 1:
                msg.append("^7%s: ^2%d ^6streaks" % (streaker, most_streak))
            if most_hs > 1:
                msg.append("^7%s: ^2%d ^1heads" % (headshooter, most_hs))
            if msg:
                game.rcon_say("^1AWARDS: %s" % " ^7- ".join(msg))

    def debug(self, msg):
        """
        print debug messages
        """
        if self.verbose:
            print msg


### CLASS Player ###
class Player(object):
    """
    Player class
    """
    teams = {0: "green", 1: "red", 2: "blue", 3: "spectator"}
    roles = {0: "Guest", 1: "User", 2: "Regular", 20: "Moderator", 40: "Admin", 60: "Full Admin", 80: "Senior Admin", 100: "Head Admin"}

    def __init__(self, player_num, ip_address, guid, name):
        """
        create a new instance of Player
        """
        self.player_num = player_num
        self.guid = guid
        self.name = "".join(name.split())
        self.aliases = []
        self.registered_user = False
        self.num_played = 0
        self.last_visit = 0
        self.admin_role = 0
        self.kills = 0
        self.db_kills = 0
        self.killing_streak = 0
        self.max_kill_streak = 0
        self.db_killing_streak = 0
        self.deaths = 0
        self.db_deaths = 0
        self.db_suicide = 0
        self.head_shots = 0
        self.db_head_shots = 0
        self.all_hits = 0
        self.tk_count = 0
        self.db_tk_count = 0
        self.db_team_death = 0
        self.tk_victim_names = []
        self.tk_killer_names = []
        self.ping_value = 0
        self.high_ping_count = 0
        self.spec_warn_count = 0
        self.warn_counter = 0
        self.flags_captured = 0
        self.flags_returned = 0
        self.address = ip_address
        self.team = 3
        self.time_joined = time.time()
        self.welcome_msg = True
        self.country = None
        self.banned_player = False

        self.prettyname = self.name
        # remove color characters from name
        for item in xrange(10):
            self.prettyname = self.prettyname.replace('^%d' % item, '')

        # check ban_list
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
        values = (self.guid, self.address, now)
        curs.execute("SELECT COUNT(*) FROM `ban_list` WHERE (`guid` = ? OR `ip_address` = ?) AND `expires` > ?", values)
        if curs.fetchone()[0] > 0:
            values = (self.guid,)
            curs.execute("SELECT `id` FROM `ban_list` WHERE `guid` = ?", values)
            result = curs.fetchone()
            game.send_rcon("^7%s ^1banned ^7(ID #%s)" % (self.name, result[0]))
            self.banned_player = True

        if not self.banned_player:
            # GeoIP lookup
            info = GEOIP.lookup(ip_address)
            if info.country:
                self.country = info.country_name
                game.rcon_say("^7%s ^7connected from %s" % (name, info.country_name))

    def ban(self, duration=900, reason='tk', admin=None):
        if admin:
            reason = "%s, ban by %s" % (reason, admin)
        unix_expiration = duration + time.time()
        expire_date = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(unix_expiration))
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
        values = (self.guid, self.prettyname, self.address, expire_date, timestamp, reason)
        curs.execute("INSERT INTO `ban_list` (`guid`,`name`,`ip_address`,`expires`,`timestamp`,`reason`) VALUES (?,?,?,?,?,?)", values)
        conn.commit()
        game.kick_player(self.player_num)

    def reset(self):
        self.kills = 0
        self.killing_streak = 0
        self.max_kill_streak = 0
        self.deaths = 0
        self.head_shots = 0
        self.all_hits = 0
        self.tk_count = 0
        self.tk_victim_names = []
        self.tk_killer_names = []
        self.warn_counter = 0
        self.flags_captured = 0
        self.flags_returned = 0

    def reset_flag_stats(self):
        self.flags_captured = 0
        self.flags_returned = 0

    def save_info(self):
        if self.registered_user:
            if self.db_deaths == 0:
                ratio = 1.0
            else:
                ratio = round(float(self.db_kills) / float(self.db_deaths), 2)
            values = (self.db_kills, self.db_deaths, self.db_head_shots, self.db_tk_count, self.db_team_death, self.db_killing_streak, self.db_suicide, ratio, self.guid)
            curs.execute("UPDATE `xlrstats` SET `kills` = ?,`deaths` = ?,`headshots` = ?,`team_kills` = ?,`team_death` = ?,`max_kill_streak` = ?,`suicides` = ?,`rounds` = `rounds` + 1,`ratio` = ? WHERE `guid` = ?", values)
            conn.commit()

    def check_database(self):
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
        # check player table
        values = (self.guid,)
        curs.execute("SELECT COUNT(*) FROM `player` WHERE `guid` = ?", values)
        if curs.fetchone()[0] < 1:
            values = (self.guid, self.prettyname, self.address, now, self.prettyname)
            curs.execute("INSERT INTO `player` (`guid`,`name`,`ip_address`,`time_joined`,`aliases`) VALUES (?,?,?,?,?)", values)
            conn.commit()
            self.aliases.append(self.prettyname)
        else:
            values = (self.prettyname, self.address, now, self.guid)
            curs.execute("UPDATE `player` SET `name` = ?,`ip_address` = ?,`time_joined` = ? WHERE `guid` = ?", values)
            conn.commit()
            # get known aliases
            values = (self.guid,)
            curs.execute("SELECT `aliases` FROM `player` WHERE `guid` = ?", values)
            result = curs.fetchone()
            # create list of aliases
            self.aliases = result[0].split(', ')
            if self.prettyname not in self.aliases:
                # add new alias to list
                if len(self.aliases) < 15:
                    self.aliases.append(self.prettyname)
                    alias_string = ', '.join(self.aliases)
                    values = (alias_string, self.guid)
                    curs.execute("UPDATE `player` SET `aliases` = ? WHERE `guid` = ?", values)
                    conn.commit()
        # check XLRSTATS table
        values = (self.guid,)
        curs.execute("SELECT COUNT(*) FROM `xlrstats` WHERE `guid` = ?", values)
        if curs.fetchone()[0] < 1:
            self.registered_user = False
        else:
            self.registered_user = True
            # get DB DATA for XLRSTATS
            values = (self.guid,)
            curs.execute("SELECT `last_played`,`num_played`,`kills`,`deaths`,`headshots`,`team_kills`,`team_death`,`max_kill_streak`,`suicides`,`admin_role` FROM `xlrstats` WHERE `guid` = ?", values)
            result = curs.fetchone()
            self.last_visit = result[0]
            self.num_played = result[1]
            self.db_kills = result[2]
            self.db_deaths = result[3]
            self.db_head_shots = result[4]
            self.db_tk_count = result[5]
            self.db_team_death = result[6]
            self.db_killing_streak = result[7]
            self.db_suicide = result[8]
            self.admin_role = result[9]
            # update name, last_played and increase num_played counter
            values = (self.prettyname, now, self.guid)
            curs.execute("UPDATE `xlrstats` SET `name` = ?,`last_played` = ?,`num_played` = `num_played` + 1 WHERE `guid` = ?", values)
            conn.commit()

    def register_user_db(self, role=1):
        if not self.registered_user:
            now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
            values = (self.guid, self.prettyname, self.address, now, now, role)
            curs.execute("INSERT INTO `xlrstats` (`guid`,`name`,`ip_address`,`first_seen`,`last_played`,`num_played`,`admin_role`) VALUES (?,?,?,?,?,1,?)", values)
            conn.commit()
            self.registered_user = True
            self.admin_role = role
            self.welcome_msg = False

    def get_banned_player(self):
        return self.banned_player

    def set_name(self, name):
        self.name = "".join(name.split())

    def get_name(self):
        return self.name

    def get_aliases(self):
        if len(self.aliases) == 15:
            self.aliases.append("and more...")
        return str(", ".join(self.aliases))

    def set_guid(self, guid):
        self.guid = guid

    def get_guid(self):
        return self.guid

    def get_player_num(self):
        return self.player_num

    def set_team(self, team):
        self.team = team

    def get_team(self):
        return self.team

    def get_num_played(self):
        return self.num_played

    def get_last_visit(self):
        return str(self.last_visit)

    def get_db_kills(self):
        return self.db_kills

    def get_kills(self):
        return self.kills

    def get_db_deaths(self):
        return self.db_deaths

    def get_deaths(self):
        return self.deaths

    def get_db_headshots(self):
        return self.db_head_shots

    def get_headshots(self):
        return self.head_shots

    def disable_welcome_msg(self):
        self.welcome_msg = False

    def get_welcome_msg(self):
        return self.welcome_msg

    def get_country(self):
        return self.country

    def get_registered_user(self):
        return self.registered_user

    def set_admin_role(self, role):
        self.admin_role = role

    def get_admin_role(self):
        return self.admin_role

    def get_ip_address(self):
        return self.address

    def get_time_joined(self):
        return self.time_joined

    def get_max_kill_streak(self):
        return self.max_kill_streak

    def kill(self):
        self.killing_streak += 1
        self.kills += 1
        self.db_kills += 1

    def die(self):
        if self.killing_streak > self.max_kill_streak:
            self.max_kill_streak = self.killing_streak
        if self.max_kill_streak > self.db_killing_streak:
            self.db_killing_streak = self.max_kill_streak
        self.killing_streak = 0
        self.deaths += 1
        self.db_deaths += 1

    def suicide(self):
        self.db_suicide += 1

    def headshot(self):
        self.head_shots += 1
        self.db_head_shots += 1

    def set_all_hits(self):
        self.all_hits += 1

    def get_all_hits(self):
        return self.all_hits

    def get_killing_streak(self):
        return self.killing_streak

    def get_db_tks(self):
        return self.db_tk_count

    def get_team_kill_count(self):
        return self.tk_count

    def add_killed_me(self, killer):
        self.tk_killer_names.append(killer)

    def get_killed_me(self):
        return self.tk_killer_names

    def clear_killed_me(self, victim):
        while self.tk_victim_names.count(victim) > 0:
            self.tk_victim_names.remove(victim)

    def add_tk_victims(self, victim):
        self.tk_victim_names.append(victim)

    def clear_tk(self, killer):
        while self.tk_killer_names.count(killer) > 0:
            self.tk_killer_names.remove(killer)

    def clear_all_tk(self):
        self.tk_killer_names = []

    def add_high_ping(self, value):
        self.high_ping_count += 1
        self.ping_value = value

    def clear_high_ping(self):
        self.high_ping_count = 0

    def get_high_ping(self):
        return self.high_ping_count

    def get_ping_value(self):
        return self.ping_value

    def add_spec_warning(self):
        self.spec_warn_count += 1

    def clear_spec_warning(self):
        self.spec_warn_count = 0

    def get_spec_warning(self):
        return self.spec_warn_count

    def add_warning(self):
        self.warn_counter += 1

    def get_warning(self):
        return self.warn_counter

    def clear_warning(self):
        self.warn_counter = 0
        self.spec_warn_count = 0
        self.tk_victim_names = []
        self.tk_killer_names = []
        # clear ban_points
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
        values = (self.guid, now)
        curs.execute("DELETE FROM `ban_points` WHERE `guid` = ? and `expires` > ?", values)
        conn.commit()

    def team_death(self):
        # increase team death counter
        self.db_team_death += 1

    def team_kill(self, victim, autokick=True):
        # increase teamkill counter
        self.tk_count += 1
        self.db_tk_count += 1
        # Regular and higher will not get punished
        if self.admin_role < 2 and autokick:
            # list of players of TK victim
            self.add_tk_victims(victim.get_player_num())
            # list of players who killed victim
            victim.add_killed_me(self.player_num)
            game.rcon_tell(self.player_num, "^7Do not attack teammates, you ^1killed ^7%s" % victim.get_name())
            game.rcon_tell(victim.get_player_num(), "^7Type ^3!fp ^7to forgive ^3%s" % self.name)
            if len(self.tk_victim_names) >= 5:
                game.rcon_say("^7Player ^2%s ^7kicked for team killing" % self.name)
                # add TK ban points - 15 minutes
                self.add_ban_point('tk, auto-kick', 900)
                game.kick_player(self.player_num)
            elif len(self.tk_victim_names) == 2:
                game.rcon_tell(self.player_num, "^1WARNING ^7[^31^7]: ^7For team killing you will get kicked")
            elif len(self.tk_victim_names) == 3:
                game.rcon_tell(self.player_num, "^1WARNING ^7[^32^7]: ^7For team killing you will get kicked")
            elif len(self.tk_victim_names) == 4:
                game.rcon_tell(self.player_num, "^1WARNING ^7[^33^7]: ^7For team killing you will get kicked")

    def add_ban_point(self, point_type, duration):
        unix_expiration = duration + time.time()
        expire_date = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(unix_expiration))
        values = (self.guid, point_type, expire_date)
        # add ban_point to database
        curs.execute("INSERT INTO `ban_points` (`guid`,`point_type`,`expires`) VALUES (?,?,?)", values)
        conn.commit()
        # check amount of ban_points
        values = (self.guid, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time())))
        curs.execute("SELECT COUNT(*) FROM `ban_points` WHERE `guid` = ? AND `expires` > ?", values)
        # ban player when he gets more than 1 ban_point
        if curs.fetchone()[0] > 1:
            # ban duration multiplied by 3
            ban_duration = duration * 3
            self.ban(duration=ban_duration, reason=point_type)
            game.rcon_say("%s ^7banned for ^1%d minutes ^7for too many warnings" % (self.name, (ban_duration / 60)))

# CTF Mode
    def capture_flag(self):
        self.flags_captured += 1

    def get_flags_captured(self):
        return self.flags_captured

    def return_flag(self):
        self.flags_returned += 1

    def get_flags_returned(self):
        return self.flags_returned


### CLASS Game ###
class Game(object):
    """
    Game class
    """
    def __init__(self):
        """
        create a new instance of Game
        """
        self.all_maps_list = []
        self.next_mapname = None
        self.mapname = None
        self.maplist = []
        self.players = {}
        self.live = False
        self.rcon_handle = Rcon(CONFIG.get('server', 'server_ip'), CONFIG.get('server', 'server_port'), CONFIG.get('server', 'rcon_password'))
        if CONFIG.getboolean('rules', 'show_rules'):
            # create instance of Rules to display the rules and rotation messages
            Rules('./conf/rules.conf', CONFIG.getint('rules', 'rules_frequency'), self.rcon_handle)

        # add Spunky Bot as player 'World' to the game
        spunky_bot = Player(1022, '127.0.0.1', 'NONE', 'World')
        self.add_player(spunky_bot)

    def send_rcon(self, command):
        """
        send RCON command

        @param command: The RCON command
        @type  command: String
        """
        if self.live:
            self.rcon_handle.push(command)

    def rcon_say(self, msg):
        """
        display message in global chat

        @param msg: The message to display in global chat
        @type  msg: String
        """
        # wrap long messages into shorter list elements
        lines = textwrap.wrap(msg, 145)
        for line in lines:
            self.send_rcon('say ^3%s' % line)

    def rcon_tell(self, player_num, msg, pm_tag=True):
        """
        tell message to a specific player

        @param player_num: The player number
        @type  player_num: Integer
        @param msg: The message to display in private chat
        @type  msg: String
        @param pm_tag: Display '[pm]' (private message) in front of the message
        @type  pm_tag: bool
        """
        lines = textwrap.wrap(msg, 135)
        prefix = "^4[pm]"
        for line in lines:
            if pm_tag:
                self.send_rcon('tell %d %s ^3%s' % (player_num, prefix, line))
                prefix = ""
            else:
                self.send_rcon('tell %d ^3%s' % (player_num, line))

    def rcon_bigtext(self, msg):
        """
        display bigtext message

        @param msg: The message to display in global chat
        @type  msg: String
        """
        self.send_rcon('bigtext "%s"' % msg)

    def rcon_forceteam(self, player_num, team):
        """
        force player to given team

        @param player_num: The player number
        @type  player_num: Integer
        @param team: The team (red, blue, spectator)
        @type  team: String
        """
        self.send_rcon('forceteam %d %s' % (player_num, team))

    def kick_player(self, player_num):
        """
        kick player

        @param player_num: The player number
        @type  player_num: Integer
        """
        self.send_rcon('kick %d' % player_num)

    def go_live(self):
        """
        go live
        """
        self.live = True
        self.rcon_handle.go_live()
        self.set_all_maps()
        self.maplist = self.rcon_handle.get_mapcycle_path()
        self.set_current_map()

    def set_current_map(self):
        """
        set the current and next map in rotation
        """
        time.sleep(4)
        try:
            self.mapname = self.rcon_handle.get_quake_value('mapname')
        except KeyError:
            self.mapname = self.next_mapname

        if self.maplist:
            if self.mapname in self.maplist:
                if self.maplist.index(self.mapname) < (len(self.maplist) - 1):
                    self.next_mapname = self.maplist[self.maplist.index(self.mapname) + 1]
                else:
                    self.next_mapname = self.maplist[0]
            else:
                self.next_mapname = self.maplist[0]
        else:
            self.next_mapname = self.mapname

    def set_all_maps(self):
        """
        set a list of all available maps
        """
        all_maps = self.rcon_handle.get_rcon_output("dir map bsp")[1].split()
        all_maps.sort()
        self.all_maps_list = [maps.replace("/", "").replace(".bsp", "") for maps in all_maps if maps.startswith("/")]

    def get_all_maps(self):
        """
        get a list of all available maps
        """
        return self.all_maps_list

    def add_player(self, player):
        """
        add a player to the game

        @param player: The instance of the player
        @type  player: Instance
        """
        with players_lock:
            self.players[player.get_player_num()] = player
            player.check_database()

    def get_gamestats(self):
        """
        get number of players in red team, blue team and spectator
        """
        game_data = {Player.teams[1]: 0, Player.teams[2]: 0, Player.teams[3]: 0}
        for player in self.players.itervalues():
            # red team
            if player.get_team() == 1:
                game_data[Player.teams[1]] += 1
            # blue team
            elif player.get_team() == 2:
                game_data[Player.teams[2]] += 1
            # spectators
            elif player.get_team() == 3:
                game_data[Player.teams[3]] += 1
        return game_data

    def balance_teams(self, game_data):
        """
        balance teams if needed

        @param game_data: Dictionary of players in each team
        @type  game_data: dict
        """
        if (game_data[Player.teams[1]] - game_data[Player.teams[2]]) > 1:
            team1 = 1
            team2 = 2
        elif (game_data[Player.teams[2]] - game_data[Player.teams[1]]) > 1:
            team1 = 2
            team2 = 1
        else:
            self.rcon_say("^7Teams are already balanced")
            return
        self.rcon_bigtext("AUTOBALANCING TEAMS...")
        num_ptm = math.floor((game_data[Player.teams[team1]] - game_data[Player.teams[team2]]) / 2)
        p_list = []

        def cmp_ab(p1, p2):
            if p1.get_time_joined() < p2.get_time_joined():
                return 1
            elif p1.get_time_joined() == p2.get_time_joined():
                return 0
            else:
                return -1
        with players_lock:
            for player in self.players.itervalues():
                if player.get_team() == team1:
                    p_list.append(player)
            p_list.sort(cmp_ab)
            for player in p_list[:int(num_ptm)]:
                self.rcon_forceteam(player.get_player_num(), Player.teams[team2])
        self.rcon_say("^7Autobalance complete!")

    def new_game(self):
        """
        set-up a new game
        """
        self.rcon_handle.clear()
        # support for low gravity server
        if CONFIG.has_section('lowgrav'):
            if CONFIG.getboolean('lowgrav', 'support_lowgravity'):
                gravity = CONFIG.getint('lowgrav', 'gravity')
                self.rcon_handle.push("set g_gravity %d" % gravity)


### Main ###
print "\n\nStarting Spunky Bot:"

# read settings.conf file
CONFIG = ConfigParser()
CONFIG.read('./conf/settings.conf')
print "- Imported config file 'settings.conf' successful."

players_lock = RLock()

# connect to database
conn = sqlite3.connect('./data.sqlite')
curs = conn.cursor()

# create tables if not exists
curs.execute('CREATE TABLE IF NOT EXISTS xlrstats (id INTEGER PRIMARY KEY NOT NULL, guid TEXT NOT NULL, name TEXT NOT NULL, ip_address TEXT NOT NULL, first_seen DATETIME, last_played DATETIME, num_played INTEGER DEFAULT 1, kills INTEGER DEFAULT 0, deaths INTEGER DEFAULT 0, headshots INTEGER DEFAULT 0, team_kills INTEGER DEFAULT 0, team_death INTEGER DEFAULT 0, max_kill_streak INTEGER DEFAULT 0, suicides INTEGER DEFAULT 0, ratio REAL DEFAULT 0, rounds INTEGER DEFAULT 0, admin_role INTEGER DEFAULT 1)')
curs.execute('CREATE TABLE IF NOT EXISTS player (id INTEGER PRIMARY KEY NOT NULL, guid TEXT NOT NULL, name TEXT NOT NULL, ip_address TEXT NOT NULL, time_joined DATETIME, aliases TEXT)')
curs.execute('CREATE TABLE IF NOT EXISTS ban_list (id INTEGER PRIMARY KEY NOT NULL, guid TEXT NOT NULL, name TEXT, ip_address TEXT, expires DATETIME DEFAULT 259200, timestamp DATETIME, reason TEXT)')
curs.execute('CREATE TABLE IF NOT EXISTS ban_points (id INTEGER PRIMARY KEY NOT NULL, guid TEXT NOT NULL, point_type TEXT, expires DATETIME)')
print "- Connected to database 'data.sqlite' successful."

# create instance of LogParser
LOGPARS = LogParser(CONFIG.get('server', 'log_file'), CONFIG.get('server', 'server_port'), CONFIG.getboolean('bot', 'verbose'), CONFIG.getboolean('bot', 'teamkill_autokick'))
print "- Parsing games log file '%s' successful." % CONFIG.get('server', 'log_file')

# load the GEO database and store it globally in interpreter memory
GEOIP = pygeoip.Database('./lib/GeoIP.dat')

# create instance of Game
game = Game()
print "- Added Spunky Bot successful to the game.\n"
print "Spunky Bot is running until you are closing this session or pressing CTRL + C to abort this process."
print "Note: Use the provided initscript to run Spunky Bot as daemon.\n"

# read the logfile
LOGPARS.read_log()

# close database connection
conn.close()
