"""Views for AI Dealer Assistant."""
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from catalog.models import Dealer
from core.exceptions import ValidationError
from assistant.agent import DealerAssistantAgent


class AssistantChatView(APIView):
    """
    POST /api/assistant/chat/
    Request: {"session_id": str, "dealer_id": int, "message": str}
    Response 200: {"reply": str, "cited_skus": list, "tool_calls": list, "actions": list}
    400 VALIDATION_ERROR on missing fields or unknown dealer.
    """

    def post(self, request):
        data = request.data or {}

        # Validate required fields
        session_id = data.get("session_id")
        if not session_id or not isinstance(session_id, str) or not session_id.strip():
            raise ValidationError("Field 'session_id' is required and must be a non-empty string.")

        message = data.get("message")
        if message is None or not isinstance(message, str) or not message.strip():
            raise ValidationError("Field 'message' is required and must be a non-empty string.")

        dealer_id = data.get("dealer_id")
        if dealer_id is None or isinstance(dealer_id, bool) or not isinstance(dealer_id, int):
            raise ValidationError("Field 'dealer_id' is required and must be an integer.")

        # Validate dealer exists in DB
        try:
            dealer = Dealer.objects.get(pk=dealer_id)
        except Dealer.DoesNotExist:
            raise ValidationError(f"Dealer with id '{dealer_id}' does not exist.")

        agent = DealerAssistantAgent()
        result = agent.chat(
            session_id=session_id.strip(),
            dealer_id=dealer.id,
            user_message=message.strip(),
        )

        return Response(result, status=status.HTTP_200_OK)
