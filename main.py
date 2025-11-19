from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.core.agent.message import UserMessageSegment, TextPart, AssistantMessageSegment
import json
import asyncio

@register("astrbot_plugin_infinite_dialogue", "Alan Backer", "自动总结对话历史实现无限对话", "1.0.0", "https://github.com/AlanBacker/astrbot_plugin_infinite_dialogue")
class InfiniteDialoguePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        logger.info("无限对话插件(InfiniteDialoguePlugin) 初始化成功。")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_message(self, event: AstrMessageEvent, *args, **kwargs):
        # 健壮的参数扫描
        actual_event = None
        if isinstance(event, AstrMessageEvent):
            actual_event = event
        else:
            for arg in args:
                if isinstance(arg, AstrMessageEvent):
                    actual_event = arg
                    break
        
        if not actual_event:
            candidates = [event] + list(args)
            for cand in candidates:
                if hasattr(cand, "message_obj"):
                    actual_event = cand
                    break
        
        if not actual_event:
            logger.error(f"无法在参数中找到 AstrMessageEvent 对象。")
            return

        event = actual_event

        # 0. 检查白名单
        whitelist = self.config.get("whitelist", [])
        if whitelist:
            current_id = ""
            if event.message_obj.group_id:
                current_id = event.message_obj.group_id
            elif event.message_obj.sender and hasattr(event.message_obj.sender, 'user_id'):
                current_id = event.message_obj.sender.user_id
            
            whitelist_str = [str(x) for x in whitelist]
            if str(current_id) not in whitelist_str:
                return

        # 1. 获取对话管理器和当前对话
        conv_mgr = self.context.conversation_manager
        try:
            uid = event.unified_msg_origin
            curr_cid = await conv_mgr.get_curr_conversation_id(uid)
            conversation = await conv_mgr.get_conversation(uid, curr_cid)
        except Exception as e:
            logger.error(f"获取对话失败: {e}")
            return

        if not conversation:
            return

        # 2. 检查历史记录长度
        messages = []
        if conversation.history:
            try:
                messages = json.loads(conversation.history)
            except:
                pass
        
        current_length = len(messages)
        max_len = self.config.get("max_conversation_length", 40)

        if current_length >= max_len:
            logger.info(f"当前对话长度 {current_length} 已达到阈值 {max_len}。正在触发总结...")
            
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
                "4. **语气**：使用客观、陈述性的语气。\n\n"
                f"对话记录：\n{history_text}"
            )
            
            # 获取配置
            target_provider_id = self.config.get("summary_provider_id")
            max_retries = self.config.get("max_retries", 3)
            
            # 获取当前会话的 Provider ID (作为 Fallback 或 默认)
            current_provider_id = None
            try:
                current_provider_id = await self.context.get_current_chat_provider_id(umo=uid)
            except Exception as e:
                logger.error(f"获取当前模型提供商 ID 失败: {e}")

            summary = None
            success = False

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
                    break # 无法重试

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
                            success = True
                            break # 跳出 providers 循环
                    except Exception as e:
                        logger.warning(f"使用提供商 {pid} 生成总结失败: {e}")
                        # 继续尝试下一个 provider (Fallback)
                
                if success:
                    break # 跳出重试循环
            
            if not success:
                logger.error("所有重试均失败。放弃本次总结。")
                # 发送警告给用户
                try:
                    from astrbot.core.agent.message import Plain
                    await self.context.send_message(event.unified_msg_origin, [Plain("【无限对话插件警告】\n总结系统故障，无法连接到模型提供商。\n本次总结已放弃，对话历史将保留。请检查模型配置或网络连接。")])
                except Exception as e:
                    logger.error(f"发送警告消息失败: {e}")
                
                return # 放弃总结，保留历史

            # 清理历史记录并将总结注入数据库
            if summary:
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
