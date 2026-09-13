"""Pytest plugin preventing network access in the local regression watcher."""
import socket


def pytest_configure(config):
    def deny_network(*args, **kwargs):
        raise RuntimeError("Network access is disabled for deterministic regression tests")

    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    socket.socket.sendto = deny_network
    socket.create_connection = deny_network
