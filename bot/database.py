from typing import Optional, Any, List, Dict
import pymongo
from pymongo import UpdateOne
import uuid
from datetime import datetime, timedelta
import config
from subscription import SubscriptionType
from subscription_config import SubscriptionConfig


class Database:
    def __init__(self):
        self.client = pymongo.MongoClient(config.mongodb_uri)
        self.db = self.client["chatgpt_telegram_bot"]

        self.user_collection = self.db["user"]
        self.subscription_collection = self.db["subscriptions"]
        self.dialog_collection = self.db["dialog"]
        self.payment_collection = self.db["payments"]

        # Создаем индексы для часто используемых запросов
        self._create_indexes()

    def _create_indexes(self):
        """Создание индексов для оптимизации запросов"""
        self.user_collection.create_index("username")
        self.user_collection.create_index("role")
        self.user_collection.create_index("last_interaction")

        self.subscription_collection.create_index([
            ("user_id", pymongo.ASCENDING),
            ("expires_at", pymongo.ASCENDING)
        ])
        self.subscription_collection.create_index("expires_at")

        self.dialog_collection.create_index([
            ("user_id", pymongo.ASCENDING),
            ("_id", pymongo.ASCENDING)
        ])

        self.payment_collection.create_index("payment_id", unique=True)
        self.payment_collection.create_index([
            ("user_id", pymongo.ASCENDING),
            ("status", pymongo.ASCENDING)
        ])

    def check_if_user_exists(self, user_id: int, raise_exception: bool = False) -> bool:
        """Проверка существования пользователя с оптимизацией через find_one"""
        user = self.user_collection.find_one({"_id": user_id}, {"_id": 1})

        if user:
            return True
        else:
            if raise_exception:
                raise ValueError(f"User {user_id} does not exist")
            return False

    def add_new_user(
            self,
            user_id: int,
            chat_id: int,
            username: str = "",
            first_name: str = "",
            last_name: str = "",
    ) -> bool:
        """Добавление нового пользователя с проверкой через insert_one"""
        user_dict = {
            "_id": user_id,
            "chat_id": chat_id,

            "username": username,
            "first_name": first_name,
            "last_name": last_name,

            "last_interaction": datetime.now(),
            "first_seen": datetime.now(),

            "current_dialog_id": None,
            "current_chat_mode": "default",
            "current_model": config.models["available_text_models"][3],
            "image_preferences": {
                "model": config.models["available_image_models"][0],
                "quality": "standard",
                "resolution": "1024x1024",
                "n_images": 1
            },

            "n_used_tokens": {},
            "total_spent": 0,
            "dalle_2": {"images": 0, "cost": 0.0},
            "dalle_3": {"images": 0, "cost": 0.0},
            "n_generated_images": 0,
            "n_transcribed_seconds": 0.0,
            "token_balance": 100000,
            "role": "trial_user",
            "euro_balance": 1,
            "rub_balance": 100,
            "total_topup": 0,
            "total_donated": 0
        }

        try:
            self.user_collection.insert_one(user_dict)
            return True
        except pymongo.errors.DuplicateKeyError:
            return False

    def _get_user_document(self, user_id: int, projection: Optional[Dict] = None) -> Dict:
        """Внутренний метод для получения документа пользователя с обработкой ошибок"""
        user = self.user_collection.find_one({"_id": user_id}, projection)
        if not user:
            raise ValueError(f"User {user_id} does not exist")
        return user

    def start_new_dialog(self, user_id: int) -> str:
        """Создание нового диалога с оптимизированными проверками"""
        self.check_if_user_exists(user_id, raise_exception=True)

        subscription_info = self.get_user_subscription_info(user_id)
        if not subscription_info["is_active"]:
            raise PermissionError("Нет активной подписки")

        dialog_id = str(uuid.uuid4())
        current_time = datetime.now()

        dialog_dict = {
            "_id": dialog_id,
            "user_id": user_id,
            "chat_mode": self.get_user_attribute(user_id, "current_chat_mode"),
            "start_time": current_time,
            "model": self.get_user_attribute(user_id, "current_model"),
            "messages": []
        }

        # Используем bulk operations для атомарности
        operations = [
            UpdateOne(
                {"_id": user_id},
                {"$set": {"current_dialog_id": dialog_id, "last_interaction": current_time}}
            )
        ]

        self.dialog_collection.insert_one(dialog_dict)
        self.user_collection.bulk_write(operations)

        return dialog_id

    def get_user_attribute(self, user_id: int, key: str) -> Any:
        """Получение атрибута пользователя с проекцией для оптимизации"""
        user = self._get_user_document(user_id, {key: 1})
        return user.get(key)

    def set_user_attribute(self, user_id: int, key: str, value: Any) -> None:
        """Установка атрибута пользователя с обновлением last_interaction"""
        self.check_if_user_exists(user_id, raise_exception=True)
        self.user_collection.update_one(
            {"_id": user_id},
            {"$set": {key: value, "last_interaction": datetime.now()}}
        )

    def update_n_used_tokens(self, user_id: int, model: str, n_input_tokens: int, n_output_tokens: int) -> None:
        """Обновление счетчиков токенов с атомарными операциями"""
        update_query = {
            "$inc": {
                f"n_used_tokens.{model}.n_input_tokens": n_input_tokens,
                f"n_used_tokens.{model}.n_output_tokens": n_output_tokens
            },
            "$set": {"last_interaction": datetime.now()}
        }

        self.user_collection.update_one({"_id": user_id}, update_query, upsert=True)

    def get_dialog_messages(self, user_id: int, dialog_id: Optional[str] = None) -> List[Dict]:
        """Получение сообщений диалога с оптимизированным запросом"""
        self.check_if_user_exists(user_id, raise_exception=True)

        if dialog_id is None:
            dialog_id = self.get_user_attribute(user_id, "current_dialog_id")

        dialog = self.dialog_collection.find_one(
            {"_id": dialog_id, "user_id": user_id},
            {"messages": 1}
        )
        return dialog["messages"] if dialog else []

    def set_dialog_messages(self, user_id: int, dialog_messages: List[Dict], dialog_id: Optional[str] = None) -> None:
        """Установка сообщений диалога"""
        self.check_if_user_exists(user_id, raise_exception=True)

        if dialog_id is None:
            dialog_id = self.get_user_attribute(user_id, "current_dialog_id")

        self.dialog_collection.update_one(
            {"_id": dialog_id, "user_id": user_id},
            {"$set": {"messages": dialog_messages}}
        )

    # Оптимизированные методы для работы с балансами
    def get_user_role(self, user_id: int) -> str:
        """Получение роли пользователя с кэшированием в будущем"""
        user = self.user_collection.find_one(
            {"_id": user_id},
            {"role": 1}
        )
        return user.get("role", "trial_user") if user else "trial_user"

    def get_user_model(self, user_id: int) -> str:
        """Получение модели пользователя"""
        user = self.user_collection.find_one(
            {"_id": user_id},
            {"current_model": 1}
        )
        return user.get("current_model", "gpt-3.5-turbo") if user else "gpt-3.5-turbo"

    def get_user_last_interaction(self, user_id: int) -> datetime:
        """Получение времени последнего взаимодействия"""
        user = self.user_collection.find_one(
            {"_id": user_id},
            {"last_interaction": 1}
        )
        return user.get("last_interaction") if user else datetime.min

    def get_user_count(self) -> int:
        """Получение количества пользователей"""
        return self.user_collection.count_documents({})

    def get_all_user_ids(self) -> List[int]:
        """Получение всех ID пользователей с проекцией"""
        return [user["_id"] for user in self.user_collection.find({}, {"_id": 1})]

    def get_user_by_id(self, user_id: int) -> Optional[Dict]:
        """Получение пользователя по ID"""
        return self.user_collection.find_one({"_id": user_id})

    def get_users_and_roles(self) -> List[Dict]:
        """Получение пользователей и их ролей с проекцией"""
        return list(self.user_collection.find(
            {},
            {"username": 1, "first_name": 1, "role": 1, "last_interaction": 1}
        ))

    def find_users_by_role(self, role: str) -> List[Dict]:
        """Поиск пользователей по роли"""
        return list(self.user_collection.find({"role": role}))

    def find_user_by_username(self, username: str) -> Optional[Dict]:
        """Поиск пользователя по username"""
        return self.user_collection.find_one({"username": username})

    def find_users_by_first_name(self, first_name: str) -> List[Dict]:
        """Поиск пользователей по имени"""
        return list(self.user_collection.find({"first_name": first_name}))

    # Оптимизированные методы работы с финансами
    def get_user_financials(self, user_id: int) -> Dict[str, float]:
        """Получение финансовой информации пользователя"""
        user_data = self.user_collection.find_one(
            {"_id": user_id},
            {"total_topup": 1, "total_donated": 1}
        )
        return {
            "total_topup": user_data.get("total_topup", 0) if user_data else 0,
            "total_donated": user_data.get("total_donated", 0) if user_data else 0
        }

    def add_subscription(self, user_id: int, subscription_type: SubscriptionType,
                         duration_days: int) -> None:
        """Добавление подписки пользователю"""
        purchased_at = datetime.now()
        expires_at = purchased_at + timedelta(days=duration_days)

        subscription_data = {
            "user_id": user_id,
            "type": subscription_type.value,
            "purchased_at": purchased_at,
            "expires_at": expires_at,
            "requests_used": 0,
            "images_used": 0
        }

        self.subscription_collection.insert_one(subscription_data)

    def get_active_subscription(self, user_id: int) -> Optional[Dict]:
        """Получение активной подписки пользователя"""
        return self.subscription_collection.find_one({
            "user_id": user_id,
            "expires_at": {"$gt": datetime.now()}
        }, sort=[("purchased_at", pymongo.DESCENDING)])

    def update_subscription_usage(self, user_id: int, request_used: bool = False,
                                  image_used: bool = False) -> None:
        """Обновление счетчиков использования подписки"""
        update_data = {}
        if request_used:
            update_data["$inc"] = {"requests_used": 1}
        if image_used:
            update_data["$inc"] = update_data.get("$inc", {})
            update_data["$inc"]["images_used"] = 1

        if update_data:
            self.subscription_collection.update_one(
                {"user_id": user_id, "expires_at": {"$gt": datetime.now()}},
                update_data
            )

    def get_user_subscription_info(self, user_id: int) -> Dict:
        """Получение информации о подписке пользователя"""
        subscription = self.get_active_subscription(user_id)
        if subscription:
            return {
                "type": subscription["type"],
                "expires_at": subscription["expires_at"],
                "requests_used": subscription.get("requests_used", 0),
                "images_used": subscription.get("images_used", 0),
                "is_active": True
            }
        else:
            return {
                "type": "free",
                "is_active": False,
                "requests_used": 0,
                "images_used": 0
            }

    # Методы платежей с улучшенной обработкой ошибок
    def create_payment(self, user_id: int, payment_id: str, amount: float,
                       payment_type: str, description: str = "") -> None:
        """Создание записи о платеже"""
        current_time = datetime.now()
        payment_data = {
            "user_id": user_id,
            "payment_id": payment_id,
            "amount": amount,
            "currency": "RUB",
            "type": payment_type,
            "description": description,
            "status": "pending",
            "created_at": current_time,
            "updated_at": current_time
        }

        try:
            self.payment_collection.insert_one(payment_data)
        except pymongo.errors.DuplicateKeyError:
            raise ValueError(f"Payment with ID {payment_id} already exists")

    def update_payment_status(self, payment_id: str, status: str) -> None:
        """Обновление статуса платежа"""
        self.payment_collection.update_one(
            {"payment_id": payment_id},
            {
                "$set": {
                    "status": status,
                    "updated_at": datetime.now()
                }
            }
        )

    def get_pending_payments(self) -> List[Dict]:
        """Получение pending платежей"""
        return list(self.payment_collection.find({
            "status": {"$in": ["pending", "waiting_for_capture"]}
        }))

    def get_payment_by_id(self, payment_id: str) -> Optional[Dict]:
        """Получение платежа по ID"""
        return self.payment_collection.find_one({"payment_id": payment_id})

    def get_user_pending_payments(self, user_id: int) -> List[Dict]:
        """Получение pending платежей пользователя"""
        return list(self.payment_collection.find({
            "user_id": user_id,
            "status": {"$in": ["pending", "waiting_for_capture"]}
        }))