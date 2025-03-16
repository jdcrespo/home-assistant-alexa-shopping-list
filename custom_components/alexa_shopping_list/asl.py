#!/usr/bin/env python3

import websockets
import json
import datetime
import os
import asyncio
import hashlib

# ============================================================


class AlexaShoppingListSync:

    def __init__(self, ip="localhost", port=4000, sync_mins=60, hasl_path=None, hasl_refresh=None, logger=None):
        self.uri = "ws://"+ip+":"+str(port)
        self._hasl_path = hasl_path
        self._hasl_refresh = hasl_refresh
        self._setup_cached_list(sync_mins * 60)
        self._is_syncing = False
        self._logger = logger
        self._local_has_changed = False

    # ============================================================
    # Helpers


    async def _send_command(self, command, **kwargs):
        async with websockets.connect(self.uri) as websocket:
            request = {
                'command': command,
                'args': {
                    **kwargs
                }
            }
            await websocket.send(json.dumps(request))
            response = await websocket.recv()
            return json.loads(response)
    

    def _command_successful(self, response):
        if "error" in response and response['error'] != None:
            return False
        return True
    

    def _command_result(self, response):
        if "result" in response:
            return response['result']
        return None
    

    def _command_error(self, response):
        if "error" in response:
            return response['error']
        return None
        

    # ============================================================
    # Server


    async def can_ping_server(self):
        response = await self._send_command("ping")
        if self._command_successful(response):
            if self._command_result(response) == "pong":
                return True
        return False
    

    async def server_config_is_valid(self):
        response = await self._send_command("config_valid")
        if self._command_successful(response):
            return self._command_result(response)
        return False
    

    async def server_is_authenticated(self):
        response = await self._send_command("authenticated")
        if self._command_successful(response):
            return self._command_result(response)
        return False

    # ============================================================
    # Cache


    def _setup_cached_list(self, sync_seconds):
        self._sync_seconds = sync_seconds
        self.last_updated = None
        self._cached_list = []
    

    def _update_cached_list(self, new_list):
        self._cached_list = new_list
        self.last_updated = datetime.datetime.now().astimezone()
    

    def _cached_list_needs_updating(self):
        if self.last_updated == None:
            return True

        now = datetime.datetime.now().astimezone()
        diff = now - self.last_updated

        if diff.total_seconds() >= self._sync_seconds:
            return True
        return False


    # ============================================================
    # Commands


    async def _get_list(self, force = False):
        if self._cached_list_needs_updating() or force:
            response = await self._send_command("get_list")
            if self._command_successful(response):
                self._update_cached_list(self._command_result(response))
        return self._cached_list
    

    async def _add_item(self, item):
        response = await self._send_command("add_item", item=item)
        if self._command_successful(response):
            self._update_cached_list(self._command_result(response))
        return self._cached_list
    

    async def _update_item(self, old, new):
        response = await self._send_command("update_item", old=old, new=new)
        if self._command_successful(response):
            self._update_cached_list(self._command_result(response))
        return self._cached_list
    

    async def _remove_item(self, item):
        response = await self._send_command("remove_item", item=item)
        if self._command_successful(response):
            self._update_cached_list(self._command_result(response))
        return self._cached_list

    # ============================================================
    # Sync


    async def homeassistant_shopping_list_updated(self, event):
        if self._local_has_changed == False:
            await self._debug_log_entry("shopping list updated, setting update flag!!")
            self._local_has_changed = True
    

    def _export_ha_shopping_list(self, items):
        export = []
        for item in items:
            export.append({
                "id": item.replace(" ", "_"),
                "name": item,
                "complete": False
            })
        
        with open(self._hasl_path, "w") as outfile:
            outfile.write(json.dumps(export, indent=4))
    

    def _read_ha_shopping_list(self):
        if os.path.exists(self._hasl_path):
            with open(self._hasl_path, 'r') as file:
                return json.load(file)
        return []
    

    def _ha_shopping_list_hash(self):
        serialized = json.dumps(self._read_ha_shopping_list(), sort_keys=True)
        return hashlib.md5(serialized.encode('utf-8')).hexdigest()
    

    def _find_ha_list_item(self, find, ha_list):
        for item in ha_list:
            if item['name'] == find:
                return item
        return None
    

    async def _debug_log_entry(self, entry=""):
        if self._logger == None:
            return
        self._logger.debug(entry)


    async def sync(self, force=False, toAlexa=False):
        await self._debug_log_entry(f"starting sync, (has_local_changes: {self._local_has_changed})")
        
        if self._cached_list_needs_updating() == False and force == False and self._local_has_changed == False:
            return False
        
        if self._is_syncing == True:
            return False
        self._is_syncing = True

        await self._debug_log_entry("can sync")

        loop = asyncio.get_running_loop()
        ha_list = await loop.run_in_executor(None, self._read_ha_shopping_list)
        original_ha_list_hash = await loop.run_in_executor(None, self._ha_shopping_list_hash)
        await self._debug_log_entry("ha list: "+json.dumps(ha_list))
        
        await self._debug_log_entry( "Loading Alexa shopping list")
        alexa_list = await self._get_list(force)
        await self._debug_log_entry("Alexa list: "+json.dumps(alexa_list))

        to_add = []
        to_remove = []

        for item in ha_list:
            if item['complete'] == True:
                if item['name'] in alexa_list:
                    to_remove.append(item['name'])
                
            if item['name'] not in alexa_list and (toAlexa or self._local_has_changed):
                to_add.append(item['name'])
        
        await self._debug_log_entry("To add to alexa: "+json.dumps(to_add))
        for item in to_add:
            await self._add_item(item)
        
        await self._debug_log_entry("To remove from alexa: "+json.dumps(to_remove))
        for item in to_remove:
            await self._remove_item(item)
        
        refreshed_items = await self._get_list()
        await self._debug_log_entry("Refreshed Alexa list: "+json.dumps(refreshed_items))
        await self._debug_log_entry("Exporting new HA shopping list")
        await loop.run_in_executor(None, self._export_ha_shopping_list, refreshed_items)
        await self._hasl_refresh()

        self._is_syncing = False
        if self._local_has_changed:
            self._local_has_changed = False

        await self._debug_log_entry("Original list hash: "+original_ha_list_hash)
        new_ha_list_hash = await loop.run_in_executor(None, self._ha_shopping_list_hash)
        await self._debug_log_entry("New list hash: "+new_ha_list_hash)
        if original_ha_list_hash != new_ha_list_hash:
            await self._debug_log_entry("List changed")
            return True
        else:
            await self._debug_log_entry("List did not change")
            return False


    # ============================================================

