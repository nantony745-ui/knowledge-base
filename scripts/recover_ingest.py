from app.ingest.recovery import recover

if __name__ == '__main__':
    print('已恢复为明确失败状态：', recover())
