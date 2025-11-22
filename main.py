from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.core.agent.message import UserMessageSegment, TextPart, AssistantMessageSegment
import json
import asyncio

@register("astrbot_plugin_infinite_dialogue", "Alan Backer", "自动总结对话历史实现无限对话", "1.0.9", "https://github.com/AlanBacker/astrbot_plugin_infinite_dialogue")
class InfiniteDialoguePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        logger.info("无限对话插件(InfiniteDialoguePlugin) v1.0.9 初始化成功。")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_message(self, event: AstrMessageEvent, *args, **kwargs):
        # 简化事件对象获取逻辑
        message_event = event  # 假设框架保证event参数是正确的事件对象

        if message_event and self._is_event_valid(message_event):
            # 检查白名单
            if not self._check_whitelist(message_event):
                return

            # 获取对话
            conversation = await self._get_conversation(message_event)
            if conversation and conversation.history:
                try:
                    # 检查是否需要总结
                    if self._should_summarize(conversation):
                        # 生成总结
                        summary_text = await self._generate_summary(conversation, message_event)
                        if summary_text:
                            # 应用总结（清理历史并注入）
                            await self._apply_summary(message_event, conversation, summary_text)
                except json.JSONDecodeError as e:
                    logger.error(f"JSON解析失败: {e}")
                except Exception as e:
                    logger.error(f"处理消息时发生错误: {e}")
            
            # 这里的 process_message 在 AstrBot 插件机制中通常是隐式的，
            # 只要我们修改了 event 对象，后续的处理流程就会使用修改后的对象。
            # await process_message(message_event) 
        else:
            logger.warning("无效的事件对象")

    def _is_event_valid(self, event: AstrMessageEvent) -> bool:
        """检查事件对象是否有效"""
        return hasattr(event, 'message_obj') and hasattr(event, 'unified_msg_origin')

    def _check_whitelist(self, event: AstrMessageEvent) -> bool:
        """检查白名单"""
        whitelist = self.config.get("whitelist", [])
        if not whitelist:
            return True
            
        current_id = ""
        if event.message_obj.group_id:
            current_id = event.message_obj.group_id
        elif event.message_obj.sender and hasattr(event.message_obj.sender, 'user_id'):
            current_id = event.message_obj.sender.user_id
        
        whitelist_str = [str(x) for x in whitelist]
        return str(current_id) in whitelist_str

    async def _get_conversation(self, event: AstrMessageEvent):
        """获取当前对话对象"""
        conv_mgr = self.context.conversation_manager
        try:
            uid = event.unified_msg_origin
            curr_cid = await conv_mgr.get_curr_conversation_id(uid)
            return await conv_mgr.get_conversation(uid, curr_cid)
        except Exception as e:
            logger.error(f"获取对话失败: {e}")
            return None

    def _should_summarize(self, conversation) -> bool:
        """判断是否需要总结"""
        messages = []
        try:
            messages = json.loads(conversation.history)
        except:
            pass
        
        current_length = len(messages)
        max_len = self.config.get("max_conversation_length", 40)
        
        if current_length >= max_len:
            logger.info(f"当前对话长度 {current_length} 已达到阈值 {max_len}。正在触发总结...")
            return True
        return False

    async def _generate_summary(self, conversation, event: AstrMessageEvent) -> str:
        """生成对话总结"""
        messages = []
        try:
            messages = json.loads(conversation.history)
        except:
            pass

        # 将当前消息临时加入到历史记录中，以便总结包含最新上下文
        current_msg_content = event.message_str
        if not current_msg_content and event.message_obj.message:
            current_msg_content = "".join([p.text for p in event.message_obj.message if isinstance(p, TextPart)])
        
        if current_msg_content:
            messages.append({"role": "user", "content": current_msg_content})

        # 准备总结内容
        history_text = ""
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            history_text += f"{role}: {content}\n"
        
        summary_prompt = (
            "请作为第三方观察者，对以下对话历史进行高度概括的总结。你的总结将被用作AI的长期记忆，帮助AI在后续对话中无缝衔接。\n"
            "要求：\n"
            "1. **字数限制**：控制在 500 字以内，言简意赅。\n"
            "2. **格式要求**：请直接输出总结内容，不要包含任何开场白或结束语。总结内容必须以“【前情提要】”开头。\n"
            "3. **内容重点**：\n"
            "   - 参与者的身份、称呼及关系。\n"
            "   - 已完成的关键任务、达成的共识或重要决策。\n"
            "   - 当前正在进行但未完成的话题或任务。\n"
            "   - 重要的上下文约束（如用户偏好、设定的场景规则等）。\n"
            "   - AI（你）在总结中则表述你自己的身份，这将会给未来的你自己看。\n"
            "   - 并且要让未来的你自己明白这个前情提要并非来自用户所为，而是全自动总结。\n"
            "   - 在结尾添加你最后说了什么，用户最后说了什么。\n"
            "4. **语气**：使用客观、陈述性的语气。\n\n"
            f"对话记录：\n{history_text}"
        )
        
        # 获取配置
        target_provider_id = self.config.get("summary_provider_id")
        max_retries = self.config.get("max_retries", 3)
        uid = event.unified_msg_origin
        
        # 获取当前会话的 Provider ID
        current_provider_id = None
        try:
            current_provider_id = await self.context.get_current_chat_provider_id(umo=uid)
        except Exception as e:
            logger.error(f"获取当前模型提供商 ID 失败: {e}")

        summary = None

        # 重试循环
        for i in range(max_retries):
            logger.info(f"正在尝试生成总结 (第 {i+1}/{max_retries} 次)...")
            
            # 确定本次尝试使用的 Provider
            providers_to_try = []
            if target_provider_id:
                providers_to_try.append(target_provider_id)
                if current_provider_id and current_provider_id != target_provider_id:
                    providers_to_try.append(current_provider_id)
            elif current_provider_id:
                providers_to_try.append(current_provider_id)
            
            if not providers_to_try:
                logger.error("未找到可用的模型提供商 ID。")
                break 

            for pid in providers_to_try:
                try:
                    logger.info(f"正在使用提供商 {pid} 生成总结...")
                    llm_resp = await self.context.llm_generate(
                        chat_provider_id=pid,
                        prompt=summary_prompt,
                        contexts=[] 
                    )
                    if llm_resp and llm_resp.completion_text:
                        summary = llm_resp.completion_text
                        logger.info(f"总结生成成功: {summary[:50]}...")
                        return summary
                except Exception as e:
                    logger.warning(f"使用提供商 {pid} 生成总结失败: {e}")
            
        logger.error("所有重试均失败。放弃本次总结。")
        # 发送警告给用户
        try:
            from astrbot.core.agent.message import Plain
            await self.context.send_message(event.unified_msg_origin, [Plain("【无限对话插件警告】\n总结系统故障，无法连接到模型提供商。\n本次总结已放弃，对话历史将保留。请检查模型配置或网络连接。")])
        except Exception as e:
            logger.error(f"发送警告消息失败: {e}")
            
        return None

    async def _apply_summary(self, event: AstrMessageEvent, conversation, summary: str):
        """应用总结：清理旧历史，创建新对话，注入总结"""
        conv_mgr = self.context.conversation_manager
        uid = event.unified_msg_origin
        curr_cid = getattr(conversation, "cid", None) # 假设 conversation 对象有 cid 属性，或者我们需要从其他地方获取

        # 注意：原始代码中 curr_cid 是通过 get_curr_conversation_id 获取的，这里我们需要确保能获取到
        if not curr_cid:
             curr_cid = await conv_mgr.get_curr_conversation_id(uid)

        try:
            logger.info("正在清理历史记录并将总结注入数据库...")
            
            # 1. 删除旧对话
            if hasattr(conv_mgr, "delete_conversation"):
                await conv_mgr.delete_conversation(uid, curr_cid)
                logger.info("旧对话已删除。")
            
            # 2. 创建新对话
            if hasattr(conv_mgr, "new_conversation"):
                new_conv_or_cid = await conv_mgr.new_conversation(uid)
                
                new_conv = None
                if isinstance(new_conv_or_cid, str):
                    cid = new_conv_or_cid
                    logger.info(f"新对话已启动 (CID): {cid}")
                    await asyncio.sleep(0.1)
                    new_conv = await conv_mgr.get_conversation(uid, cid)
                else:
                    new_conv = new_conv_or_cid
                    cid = getattr(new_conv, "cid", "unknown")
                    logger.info(f"新对话已启动 (Obj): {cid}")
                
                # 3. 将总结注入新对话的历史记录
                if new_conv:
                    summary_msg = {
                        "role": "assistant",
                        "content": f"【前情提要】\n{summary}"
                    }
                    new_history = [summary_msg]
                    new_conv.history = json.dumps(new_history, ensure_ascii=False)
                    
                    # 4. 保存带有注入历史的新对话
                    saved = False
                    if hasattr(conv_mgr, "save_conversation"):
                        try:
                            await conv_mgr.save_conversation(new_conv)
                            logger.info("总结已通过 save_conversation 保存。")
                            saved = True
                        except Exception as e:
                            logger.warning(f"save_conversation 失败: {e}")
                    
                    if not saved and hasattr(conv_mgr, "update_conversation"):
                        try:
                            await conv_mgr.update_conversation(new_conv)
                            logger.info("总结已保存至新对话历史。")
                        except TypeError as e:
                            if "unhashable type" in str(e):
                                logger.warning(f"update_conversation 抛出 unhashable type 错误，但这可能不影响当前会话: {e}")
                            else:
                                raise e
                    elif not saved:
                        logger.warning("无法保存新对话历史：未找到保存方法。")
                else:
                    logger.error("无法获取新对话对象。")
                    
        except Exception as e:
            logger.error(f"管理对话历史时出错: {e}")

        # 兜底方案：同时也注入到当前消息对象中
        try:
            summary_text = f"【前情提要】\n{summary}\n"
            if event.message_obj and event.message_obj.message:
                from astrbot.core.agent.message import TextPart
                if isinstance(event.message_obj.message[0], TextPart):
                    if "【前情提要】" not in event.message_obj.message[0].text:
                        event.message_obj.message[0].text = summary_text + event.message_obj.message[0].text
                else:
                    event.message_obj.message.insert(0, TextPart(text=summary_text))
            
            if hasattr(event, "message_str"):
                try:
                    new_str = "".join([p.text for p in event.message_obj.message if isinstance(p, TextPart)])
                    event.message_str = new_str
                except Exception as e:
                    pass

            logger.info("总结已注入当前消息对象。")
        except Exception as e:
            logger.error(f"注入总结到消息对象失败: {e}")
