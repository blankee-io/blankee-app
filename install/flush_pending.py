#!/usr/bin/env python3
"""
Flush every user's pending Redis writes to MySQL.

Run by the updater before it checks anything out. Redis is the primary store and
a worker moves writes to MySQL every 15 seconds, so at any moment some recent
work exists only in Redis.

Restarting the application does not by itself lose that - Redis is its own
service and outlives the reload. The reasons to do this first are narrower and
worth stating, because "just in case" tends to get optimised away later:

  - A release may carry a migration. Rows written under the old shape are better
    landed in MySQL before the shape changes than after.
  - If the update goes badly and the machine is rolled back or rebooted, whatever
    had not been flushed is the part nobody can reconstruct.
  - It runs before the checkout on purpose, so the code doing the flushing is the
    code that wrote the data.

Best effort by design. Anything it cannot flush stays in Redis and the normal
worker will take it, so a failure here is worth reporting and not worth blocking
an update over.
"""
import os
import sys


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    app_dir = os.path.dirname(here)
    sys.path.insert(0, app_dir)

    config_dir = os.environ.get('BLANKEE_CONFIG_DIR', '/var/www/budget_env')
    env_file = os.path.join(config_dir, '.env')
    try:
        from dotenv import load_dotenv
        load_dotenv(env_file)
    except Exception:
        # REDIS_* live in .env; without them the defaults below still describe a
        # standard install, so this is not fatal.
        pass

    try:
        import redis
        import redis_manager
    except Exception as e:
        print('flush: cannot load the application modules (%s)' % e)
        return 0

    try:
        client = redis.Redis(
            host=os.getenv('REDIS_HOST', '127.0.0.1'),
            port=int(os.getenv('REDIS_PORT', '6379')),
            db=int(os.getenv('REDIS_DB', '0')),
            password=os.getenv('REDIS_PASSWORD') or None,
            decode_responses=True,
            socket_timeout=5,
        )
        client.ping()
    except Exception as e:
        print('flush: no Redis to flush from (%s)' % e)
        return 0

    redis_manager.init_redis_manager(client)

    # Whoever has a dirty_tables key has work outstanding. The hydrated-user set
    # redis_manager keeps is per-process and empty here, so it cannot be used.
    users = set()
    try:
        for key in client.scan_iter(match='dirty_tables:*', count=200):
            tail = str(key).rsplit(':', 1)[-1]
            if tail.isdigit():
                users.add(int(tail))
    except Exception as e:
        print('flush: could not list pending users (%s)' % e)
        return 0

    if not users:
        print('flush: nothing pending')
        return 0

    flushed = failed = rows = 0
    for user_id in sorted(users):
        try:
            rows += (redis_manager.flush_dirty_tables_for_user(user_id) or 0)
            flushed += 1
        except Exception as e:
            failed += 1
            print('flush: user %s failed (%s)' % (user_id, e))

    print('flush: %d user(s), %d row(s)%s'
          % (flushed, rows, ', %d failed' % failed if failed else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
