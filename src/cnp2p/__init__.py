"""CNP2P: a compact educational peer-to-peer chat network."""

from .models import Peer
from .node import DeliveryReceipt, P2PNode

__all__ = ["DeliveryReceipt", "P2PNode", "Peer"]
__version__ = "0.2.0"
