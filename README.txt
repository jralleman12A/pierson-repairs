Boxlight ↔ Pierson Messaging Test

Replace these files in your current pierson-repairs repo:
- app.py
- templates/base.html
- templates/boxlight/base.html
- templates/boxlight/dashboard.html
- templates/boxlight/repairs.html
- templates/boxlight/stock.html
- templates/boxlight/repair_detail.html
- templates/boxlight/messages.html
- templates/boxlight/message_thread.html
- templates/boxlight/admin_messages.html
- templates/boxlight/admin_message_thread.html

What this adds:
- Boxlight portal "Messages" navigation item with unread count
- Boxlight can start a general message or tie it to a repair
- "Message Pierson" button on Boxlight repair detail
- Threaded replies between Boxlight and Pierson admin
- Admin "Boxlight Messages" navigation item with unread count
- Admin can reply, close, and reopen conversations
- Read/unread tracking on each side
- PostgreSQL tables are created automatically by db.create_all() on deployment

Test flow:
1. Deploy to Render.
2. Sign in as Boxlight through the shared customer login.
3. Open Messages and send a test thread.
4. Sign out and sign in as Pierson admin.
5. Open Boxlight Messages, reply, then sign back in as Boxlight.
6. Confirm the unread indicator and reply appear.

No email notifications are included yet. This is intentionally an in-app messaging test first.
