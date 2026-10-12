"""Tool definitions and executor for Dealer Assistant."""
import json
import logging
from decimal import Decimal
from django.db import connection
from catalog.models import Inventory, Product
from core.exceptions import ValidationError
from orders.services import OrderService
from assistant.retrieval import CatalogRetriever

logger = logging.getLogger(__name__)

# Tool schema declarations
TOOL_DEFINITIONS = [
    {
        "name": "search_parts",
        "description": "Search the catalogue of auto spare parts using hybrid keyword/fitment retrieval. Returns top matching products with sku, name, brand, fitment, and price.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": "Part name, type or keyword (e.g. 'brake pads', 'air filter', 'chain kit').",
                },
                "vehicle": {
                    "type": "STRING",
                    "description": "Optional vehicle model to filter fitment (e.g. 'Pulsar 150', 'Duke 390', 'Activa').",
                },
                "category": {
                    "type": "STRING",
                    "description": "Optional category filter (e.g. 'Braking', 'Electrical', 'Engine', 'Transmission').",
                },
                "limit": {
                    "type": "INTEGER",
                    "description": "Maximum number of parts to return (default: 5).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "check_stock",
        "description": "Check real-time stock quantity and current unit price for a specific product SKU. Call this whenever live stock or price is needed.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "sku": {
                    "type": "STRING",
                    "description": "The exact product SKU (e.g. 'BAJ-BP-001').",
                },
            },
            "required": ["sku"],
        },
    },
    {
        "name": "create_draft_order",
        "description": "Create a DRAFT order for the dealer with one or more items. Note: This creates a DRAFT only. Confirmation must be completed by the dealer via the portal or API.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "items": {
                    "type": "ARRAY",
                    "description": "List of line items to order.",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "sku": {
                                "type": "STRING",
                                "description": "The product SKU to order.",
                            },
                            "quantity": {
                                "type": "INTEGER",
                                "description": "Quantity to order (must be an integer >= 1).",
                            },
                        },
                        "required": ["sku", "quantity"],
                    },
                },
            },
            "required": ["items"],
        },
    },
]


def execute_tool(name: str, arguments: dict, dealer_id: int, turn_context: dict) -> dict:
    """
    Execute a tool by name, updating turn_context with tool_calls, actions, and verified figures.
    """
    if not isinstance(arguments, dict):
        try:
            arguments = json.loads(arguments) if isinstance(arguments, str) else {}
        except Exception:
            arguments = {}

    # Record tool call in turn_context in sequential order
    turn_context.setdefault("tool_calls", []).append({
        "name": name,
        "arguments": arguments,
    })

    if name == "search_parts":
        query = str(arguments.get("query", ""))
        vehicle = arguments.get("vehicle")
        category = arguments.get("category")
        limit = arguments.get("limit", 5)
        try:
            limit = int(limit)
        except (ValueError, TypeError):
            limit = 5

        products = CatalogRetriever.search(query=query, vehicle=vehicle, category=category, limit=limit)
        # Record observed prices and stock from live DB
        for p in products:
            sku = p.get("sku")
            if sku:
                turn_context.setdefault("turn_skus", set()).add(sku)
                turn_context.setdefault("turn_prices", {})[sku] = p.get("price")
                if "stock" in p:
                    turn_context.setdefault("turn_stocks", {})[sku] = p.get("stock")

        return {
            "status": "success",
            "products": products,
            "count": len(products),
            "untrusted_data": True,
        }

    elif name == "check_stock":
        sku = str(arguments.get("sku", "")).strip()
        try:
            product = Product.objects.select_related("inventory").get(sku__iexact=sku)
            stock = product.inventory.quantity if hasattr(product, "inventory") else 0
            price_str = f"{product.price:.2f}"

            turn_context.setdefault("turn_skus", set()).add(product.sku)
            turn_context.setdefault("turn_prices", {})[product.sku] = price_str
            turn_context.setdefault("turn_stocks", {})[product.sku] = stock

            return {
                "status": "success",
                "sku": product.sku,
                "name": product.name,
                "stock": stock,
                "price": price_str,
                "untrusted_data": True,
            }
        except Product.DoesNotExist:
            return {
                "status": "error",
                "message": f"Product with SKU '{sku}' not found in catalogue.",
            }

    elif name == "create_draft_order":
        items = arguments.get("items", [])
        if not isinstance(items, list):
            return {
                "status": "error",
                "message": "Items must be a list of {sku, quantity} objects.",
            }

        try:
            # Dealer id is strictly injected by the server, never from the model arguments
            order_data, status_code = OrderService.create_draft_order(
                dealer_id=dealer_id,
                items_data=items,
            )
            order_no = order_data.get("order_number")

            # Record draft order action
            turn_context.setdefault("actions", []).append({
                "type": "draft_order_created",
                "order_number": order_no,
            })

            # Record prices and items
            for item in order_data.get("items", []):
                s = item.get("sku")
                if s:
                    turn_context.setdefault("turn_skus", set()).add(s)
                    turn_context.setdefault("turn_prices", {})[s] = item.get("unit_price")

            return {
                "status": "success",
                "order_number": order_no,
                "order_status": "DRAFT",
                "grand_total": order_data.get("grand_total"),
                "subtotal": order_data.get("subtotal"),
                "tax_total": order_data.get("tax_total"),
                "items_count": len(order_data.get("items", [])),
                "message": f"Draft order {order_no} created successfully. The dealer must confirm it via the portal or API.",
            }
        except ValidationError as e:
            return {
                "status": "error",
                "message": str(e),
            }
        except Exception as e:
            logger.exception("Error creating draft order via tool")
            return {
                "status": "error",
                "message": f"Failed to create draft order: {str(e)}",
            }

    else:
        return {
            "status": "error",
            "message": f"Unknown tool: '{name}'. Available tools: search_parts, check_stock, create_draft_order.",
        }
