"""URL routes for Assistant."""
from django.urls import path
from assistant.views import AssistantChatView

urlpatterns = [
    path("assistant/chat/", AssistantChatView.as_view(), name="assistant-chat"),
]
