import time
from uuid import uuid4

import jwt
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from pwdlib import PasswordHash
from sqlalchemy import select, text

from app.api.common import fail
from app.db.models import User
from app.db.session import session
from app.settings import load_settings

router = APIRouter(prefix='/api/auth', tags=['认证'])
passwords = PasswordHash.recommended()


def initialize_admin():
    settings = load_settings()
    with session() as db:
        if db.execute(text("SELECT GET_LOCK('personal_knora_admin_init', 10)")).scalar_one() != 1:
            raise RuntimeError('管理员初始化锁获取失败')
        try:
            users = list(db.scalars(select(User)))
            if len(users) > 1:
                raise RuntimeError('数据库存在多个用户，与单管理员模式冲突')
            if not users:
                settings.require('ADMIN_USERNAME', 'ADMIN_PASSWORD')
                db.add(User(id=str(uuid4()), username=settings.ADMIN_USERNAME,
                            password_hash=passwords.hash(settings.ADMIN_PASSWORD.get_secret_value())))
                db.commit()
        finally:
            db.execute(text("SELECT RELEASE_LOCK('personal_knora_admin_init')"))


def require_origin(request: Request):
    if request.headers.get('origin') != load_settings().APP_ORIGIN:
        fail(403, 'invalid_origin', '请求Origin不匹配或缺失')


def current_user(request: Request) -> str:
    settings = load_settings()
    token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    settings.require('JWT_SECRET')
    try:
        payload = jwt.decode(token or '', settings.JWT_SECRET.get_secret_value(), algorithms=['HS256'],
                             options={'require': ['sub', 'iat', 'exp']})
    except jwt.InvalidTokenError:
        fail(401, 'unauthorized', '请登录或重新登录')
    with session() as db:
        user = db.get(User, payload['sub'])
        if user is None:
            fail(401, 'unauthorized', '登录用户不存在')
        return user.id


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


@router.post('/login', dependencies=[Depends(require_origin)])
def login(body: Login, response: Response):
    with session() as db:
        user = db.scalar(select(User).where(User.username == body.username))
        if user is None or not passwords.verify(body.password, user.password_hash):
            fail(401, 'invalid_credentials', '用户名或密码错误')
        s = load_settings()
        now = int(time.time())
        token = jwt.encode({'sub': user.id, 'iat': now, 'exp': now + s.SESSION_TTL_SECONDS}, s.JWT_SECRET.get_secret_value(), algorithm='HS256')
        response.set_cookie(s.SESSION_COOKIE_NAME, token, max_age=s.SESSION_TTL_SECONDS, httponly=True, secure=s.COOKIE_SECURE, samesite='lax', path='/')
        return {'id': user.id, 'username': user.username}


@router.post('/logout', status_code=204, dependencies=[Depends(require_origin)])
def logout(response: Response):
    s = load_settings()
    response.delete_cookie(s.SESSION_COOKIE_NAME, path='/', secure=s.COOKIE_SECURE, httponly=True, samesite='lax')


@router.get('/me')
def me(owner: str = Depends(current_user)):
    with session() as db:
        user = db.get(User, owner)
        return {'id': user.id, 'username': user.username}
