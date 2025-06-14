import platform
import logging
import os
import base64
import subprocess

from network.protocolconnection import ClientProtocolConnection
from client.OSAgents.linux import LinuxAgent
from client.OSAgents.windows import WindowsAgent

DEFAULT_TIMEOUT=5

class ConnectionKickedError(Exception):
    pass

class ClientConnection():
    def __init__(self, connection:ClientProtocolConnection):
        """
        Initializes a client connection and starts the action loop.
        
        Args:
            connection (ClientProtocolConnection): The active protocol connection.

        Raises:
            NotImplementedError: If the client's os is unsupported / not yet implemented.
        """
        self.connection = connection
        self.logger = logging.getLogger("flitifyclient")
        match platform.system():
            case "Linux":
                self.osagent = LinuxAgent()
            case "Windows":
                self.osagent = WindowsAgent()
            case _:
                raise NotImplementedError(f"Unsupported OS: {platform.system()}")
        self._actionLoop()

    def _actionLoop(self):
        """
        Main loop for receiving and handling actions sent from the server.
        Runs until connection is closed or a fatal error occurs.
        """
        while True:
            if not self.connection.running:
                self.logger.error(f'{self.connection.peerAddr}: connection closed during actionLoop')
                break
            command, commandData = self.connection.recvAction()
            self.logger.debug(f'{self.connection.peerAddr}: received command {command}')
            match command:
                case 'kick':
                    if 'reason' not in commandData:
                        raise ValueError('kicked without reason')
                    self.logger.error(f"{self.connection.peerAddr}: kicked by server: {commandData['reason']}")
                    raise ConnectionKickedError(commandData['reason'])
                    self.connection.closeConnection()
                    return
                case 'ping':
                    self.connection.sendResponse('pong', {})
                case 'get_status':
                    status_dict = self.osagent.getStatus()
                    self.connection.sendResponse('status', status_dict)
                case 'list_dir':
                    path = commandData.get('path', '/')
                    self.getDirectoryListing(path)
                case 'shell_command':
                    command = commandData.get('command')
                    if not command:
                        self.connection.sendResponse('shell_response', {'status': 'failed'})
                        raise ValueError("shell_command: command not found in server request")
                    timeout = commandData.get('timeout', DEFAULT_TIMEOUT)
                    self.executeShellCommand(command, timeout=timeout)
                case 'get_file':
                    if not 'path' in commandData:
                        self.connection.sendResponse('file_send', {'status': 'failed'})
                        raise ValueError("file_send: 'path' not found in server request")
                    self.sendFile(commandData['path'])
                case 'upload_file':
                    path = commandData.get('path')
                    filedata = commandData.get('filedata')
                    if not path or not filedata:
                        raise ValueError("upload_file: 'path' or 'filedata' missing")
                    self.saveFile(path, filedata)
                case _:
                    self.connection.sendResponse('invalid_action', {})

    def getDirectoryListing(self, path:str):
        """
        Sends a directory listing for the specified path to the server.

        Args:
            path (str): Filesystem path to list.

        Sends:
            'list_dir' response containing either a lit of entries or an error status
        """
        try:
            entries = self.osagent.getDirectoryListing(path)
            self.connection.sendResponse('list_dir', {'status': 'ok', 'entries': entries})
        except FileNotFoundError:
            self.connection.sendResponse('list_dir', {'status': 'not_found'})
            return
        except Exception as e:
            self.connection.sendResponse('list_dir', {'status': 'failed'})
            self.logger.warning(f'{self.connection.peerAddr}: list_dir failed: {e}')

    def sendFile(self, path:str):
        try:
            if not os.path.isfile(path):
                self.connection.sendResponse('file_send', {'status': 'not_found'})
                return
            fileData = open(path, 'rb').read()
            fileData = base64.b64encode(fileData).decode()
            self.connection.sendResponse('file_send', {'status': 'ok', 'filedata': fileData})
        except Exception as e:
            self.connection.sendResponse('file_send', {'status': 'failed'})
            self.logger.warning(f'{self.connection.peerAddr}: file_send failed: {e}')

    def saveFile(self, path: str, base64data: str):
        """
        Saves a file received from the server to the specified path.

        Args:
            path (str): Destination path for the file.
            base64data (str): File contents encoded in base64.

        Sends:
            'file_upload' response with status 'ok', 'file_exists', or 'failed'.
        """
        try:
            file_bytes = base64.b64decode(base64data)
            if os.path.exists(path):
                self.connection.sendResponse('file_upload', {'status': 'file_exists'})
                return
            with open(path, 'wb') as f:
                f.write(file_bytes)
            self.connection.sendResponse('file_upload', {'status': 'ok'})
        except Exception as e:
            self.connection.sendResponse('file_upload', {'status': 'failed'})
            self.logger.warning(f'{self.connection.peerAddr}: file_upload failed: {e}')

    def executeShellCommand(self, command: str, timeout=DEFAULT_TIMEOUT):
        """
        Executes a shell command and sends the result back to the server.

        Args:
            command (str): The shell command to execute.
            timeout (int): Maximum time in seconds before the command is forcibly terminated.

        Sends:
            'shell_result' response with command output, error stream, and exit code.
        """
        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
            self.connection.sendResponse('shell_result', {'status': 'ok', 'stdout': result.stdout, 'stderr': result.stderr, 'exitcode': result.returncode})
        except subprocess.TimeoutExpired:
            self.connection.sendResponse('shell_result', {'status': 'timeout', 'stderr': 'Command timed out', 'exitcode': -1})
            return
        except Exception as e:
            self.connection.sendResponse('shell_result', {'status': 'failed'})
            self.logger.warning(f'{self.connection.peerAddr}: shell_command failed: {e}')

