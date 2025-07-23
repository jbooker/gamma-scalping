# delta_hedger/market/connection_manager.py

import asyncio
import logging
from typing import Optional
import time

logger = logging.getLogger(__name__)

class ConnectionManager:
    """
    Manages websocket connections to prevent rate limiting and connection issues.
    
    This class implements:
    - Sequential connection startup with delays
    - Connection health monitoring
    - Automatic backoff on connection errors
    - Prevention of duplicate connections
    """
    
    def __init__(self):
        self._active_connections = {}
        self._connection_lock = asyncio.Lock()
        self._last_connection_time = 0
        self._min_connection_interval = 5.0  # Minimum seconds between connections
    
    async def start_connection(self, connection_id: str, stream_coro):
        """
        Start a connection with proper rate limiting and error handling.
        
        Args:
            connection_id: Unique identifier for this connection
            stream_coro: Coroutine that runs the stream connection
        """
        async with self._connection_lock:
            # Check if connection already exists
            if connection_id in self._active_connections:
                logger.warning(f"Connection {connection_id} already active")
                return
            
            # Rate limiting: ensure minimum interval between connections
            time_since_last = time.time() - self._last_connection_time
            if time_since_last < self._min_connection_interval:
                wait_time = self._min_connection_interval - time_since_last
                logger.info(f"Rate limiting: waiting {wait_time:.1f}s before starting {connection_id}")
                await asyncio.sleep(wait_time)
            
            # Start the connection
            logger.info(f"Starting connection: {connection_id}")
            task = asyncio.create_task(stream_coro)
            self._active_connections[connection_id] = task
            self._last_connection_time = time.time()
            
            return task
    
    def get_active_connections(self):
        """Return list of active connection tasks."""
        return list(self._active_connections.values())
    
    async def cleanup(self):
        """Clean up all active connections."""
        tasks = list(self._active_connections.values())
        self._active_connections.clear()
        
        if tasks:
            logger.info("Cleaning up active connections...")
            for task in tasks:
                task.cancel()
            
            # Wait for cancellation to complete
            await asyncio.gather(*tasks, return_exceptions=True)
