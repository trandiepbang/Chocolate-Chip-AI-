from fastapi import APIRouter, WebSocket, Depends, HTTPException
from sqlalchemy.orm import Session
import json
from datetime import datetime, timezone
from models.message import ChatMessage
from models.converstation import Converstation
from db import database
from schemas.response import ResponseModel
from libs.chat_service import summarize_chat_history, answer_as_an_expert
from libs.expert_selector import get_random_expert, get_expert_by_id, get_all_experts
import asyncio

router = APIRouter()

@router.get("/chat/history")
async def get_chat_history(db: Session = Depends(database.get_db)):
    chat_history = db.query(Converstation)\
        .order_by(Converstation.created_at.desc())\
        .all()
    if not chat_history:
        return ResponseModel(data=[])
    return ResponseModel(data=chat_history)

@router.get("/chat/experts")
async def get_experts():
    return ResponseModel(data=get_all_experts())

@router.get("/chat/history/{conversation_id}")
async def get_conversation_history(
    conversation_id: str, 
    db: Session = Depends(database.get_db)
):
    chat_history = db.query(ChatMessage).filter(
        ChatMessage.converstation_id == conversation_id
    ).all()
    
    if not chat_history:
        return ResponseModel(
            data=[]
        )
    return ResponseModel(data=chat_history)


def serialize_chat_message(message: ChatMessage) -> dict:
    return {
        "id": message.id,
        "role": message.role,
        "message": message.message,
        "converstation_id": message.converstation_id,
        "created_at": message.created_at.isoformat(),
        "updated_at": message.updated_at.isoformat()
    }


async def process_expert_stream(websocket, expert_id, message, history, map_id_to_message, message_ids_by_expert):
    try:
        # Await the answer_as_an_expert call here instead of in the main loop
        stream = await answer_as_an_expert(
            get_expert_by_id(expert_id),
            message,
            history
        )
        
        async for response in stream:
            if response.choices[0].finish_reason != "stop":
                content = response.choices[0].delta.content
                if response.id not in map_id_to_message:
                    map_id_to_message[response.id] = {
                        "message": content,
                        "message_id": response.id,
                        "expert": expert_id,
                    }
                    message_ids_by_expert.append(response.id)
                else:
                    map_id_to_message[response.id]["message"] += content

                await websocket.send_json({
                    "message": content,
                    "message_id": response.id,
                    "expert": get_expert_by_id(expert_id),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "is_stop": response.choices[0].finish_reason == "stop",
                    "role": "bot"
                })
                
    except Exception as e:
        print(f"Error processing expert {expert_id}: {str(e)}")
        raise

@router.websocket("/ws/chat")
async def websocket_endpoint(websocket: WebSocket, db: Session = Depends(database.get_db)):
    await websocket.accept()
    
    
    try:
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)
            chat_history = db.query(ChatMessage).filter(
                ChatMessage.converstation_id == data["converstation_id"]
            ).all()
            serialized_history = [serialize_chat_message(msg) for msg in chat_history]
            
            # Save user message to database
            user_message = ChatMessage(
                role="human",
                message=data["message"],
                converstation_id=data["converstation_id"],
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc)
            )
            db.add(user_message)
            db.commit()

            # exist converstation
            exist_converstation = db.query(Converstation).filter(
                Converstation.converstation_id == data["converstation_id"]
            ).all()
            
            converstation_payload = Converstation(
                converstation_id=data["converstation_id"],
            )

            if len(serialized_history) == 0:
                serialized_history.append(serialize_chat_message(user_message))

            if not exist_converstation:
                converstation_payload.summary = await summarize_chat_history(serialized_history)
                converstation_payload.expert = data["experts"]
                converstation_payload.created_at = datetime.now(timezone.utc)
                converstation_payload.updated_at = datetime.now(timezone.utc)
                db.add(converstation_payload)
                db.commit()
            else:
                converstation_payload = exist_converstation[0]


            map_id_to_message = {}
            message_ids_by_expert = []
            list_of_experts = converstation_payload.expert.split(",")
            if len(list_of_experts) > 0:
                tasks = []
                for expert_id in list_of_experts:
                    task = process_expert_stream(
                        websocket,
                        expert_id,
                        data["message"],
                        serialized_history,
                        map_id_to_message,
                        message_ids_by_expert
                    )
                    tasks.append(task)
            
                await asyncio.gather(*tasks)

            if len(message_ids_by_expert) > 0:
                for message_id in message_ids_by_expert:
                    bot_message = ChatMessage(
                        role="bot",
                        message=map_id_to_message[message_id]["message"],
                        converstation_id=converstation_payload.converstation_id,
                        created_at=datetime.now(timezone.utc),
                        updated_at=datetime.now(timezone.utc)
                    )
                    db.add(bot_message)

            db.commit()



    except json.JSONDecodeError as je:
        await websocket.send_text(json.dumps({
            "error": "Failed to parse JSON",
            "details": str(je),
            "status": 400
        }))
    except Exception as e:
        print("Exception ", e)
        print(e)
