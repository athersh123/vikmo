"""Models for Assistant conversation persistence."""
from django.db import models
from catalog.models import Dealer


class ConversationMessage(models.Model):
    """
    Stores turn-by-turn conversation memory keyed by session_id.
    Different sessions never share memory.
    """
    session_id = models.CharField(max_length=255, db_index=True)
    dealer = models.ForeignKey(Dealer, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=32)  # "user", "model"
    content = models.TextField()
    tool_calls = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["session_id", "created_at"]),
        ]

    def __str__(self):
        return f"{self.session_id} [{self.role}]: {self.content[:30]}"
