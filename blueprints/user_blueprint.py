import os
import json
import datetime

from passlib.hash import pbkdf2_sha512 as hash_manager

from flask import render_template, Blueprint, url_for, request, redirect, g
from flask_json import as_json, JsonError

from flask_jwt_extended import (
    jwt_required, jwt_refresh_token_required,
    create_access_token, create_refresh_token,
    get_raw_jwt, get_jti, current_user
)

from flask_utils import validate_json, id_creator

from init_app import flask_exts
from models import *

BASE_URL_LOCAL = 'http://127.0.0.1:5000'
BASE_URL_REMOTE = 'https://sc-backend-v0.herokuapp.com'
LOGIN_URL = 'https://www.sproul.club/signin'
RECOVER_URL = 'https://www.sproul.club/resetpassword'

user_blueprint = Blueprint('user', __name__, url_prefix='/api/user')


@flask_exts.jwt.user_loader_callback_loader
def user_loader_callback(identity):
    try:
        user = User.objects.get(email=identity)
        club = Club.objects.get(owner=user)
        return {'user': user, 'club': club}
    except (User.DoesNotExist, Club.DoesNotExist):
        return None


@flask_exts.jwt.user_loader_error_loader
def custom_user_loader_error(identity):
    return {'status': 'error', 'reason': 'User not found'}, 404


@flask_exts.jwt.token_in_blacklist_loader
def is_token_in_blacklist(decrypted_token):
    jti = decrypted_token['jti']
    access_jti = AccessJTI.objects(token_id=jti).first()
    refresh_jti = RefreshJTI.objects(token_id=jti).first()

    if access_jti is None and refresh_jti is None:
        return False
    elif access_jti is not None:
        return access_jti.expired
    else:
        return refresh_jti.expired


@flask_exts.jwt.expired_token_loader
def expired_jwt_handler(exp_token):
    return {'status': 'error', 'reason': 'Token has expired'}, 401

@flask_exts.jwt.unauthorized_loader
@flask_exts.jwt.invalid_token_loader
def unauth_or_invalid_jwt_handler(reason):
    return {'status': 'error', 'reason': reason}, 401

@flask_exts.jwt.revoked_token_loader
def revoked_jwt_handler():
    return {'status': 'error', 'reason': 'Token has been revoked'}, 401

@as_json
@user_blueprint.route('/email-exists', methods=['POST'])
@validate_json(schema={
    'email': {'type': 'string'}
}, require_all=True)
def does_email_exist():
    json = g.clean_json
    email = json['email']

    email_exists = PreVerifiedEmail.objects(email=email).first() is not None
    return {'exists': email_exists}


@as_json
@user_blueprint.route('/register', methods=['POST'])
@validate_json(schema={
    'name': {'type': 'string', 'minlength': 1, 'maxlength': 100},
    'email': {'type': 'string'},
    'password': {'type': 'string', 'minlength': 1},
    'tags': {'type': 'list', 'schema': {'type': 'integer'}, 'minlength': 1, 'maxlength': 3},
    'app_required': {'type': 'boolean'},
    'new_members': {'type': 'boolean'}
}, require_all=True)
def register():
    json = g.clean_json

    club_name = json['name']
    club_email = json['email']
    club_password = json['password']
    club_tag_ids = json['tags']
    app_required = json['app_required']
    new_members = json['new_members']

    # Check if email is part of pre-verified list of emails
    email_exists = PreVerifiedEmail.objects(email=club_email).first() is not None
    if not email_exists:
        raise JsonError(status='error', reason='The provided email is not part of the pre-verified list of emails!', status_=404)

    # Check if email is already registered
    user_exists = User.objects(email=club_email).first() is not None
    if user_exists:
        raise JsonError(status='error', reason='A club under that email already exists!', status_=401)

    new_user = User(
        email=club_email,
        password=hash_manager.hash(club_password)
    )

    new_club = Club(
        id=id_creator(club_name),
        name=club_name,
        owner=new_user,
        
        tags=Tag.objects.filter(id__in=club_tag_ids),
        app_required=app_required,
        new_members=new_members,
    )

    new_user.save()
    new_club.save()

    verification_token = flask_exts.email_verifier.generate_token(club_email, 'confirm-email')
    confirm_url = BASE_URL_REMOTE + url_for('user.confirm_email', token=verification_token)
    html = render_template('confirm-email.html', confirm_url=confirm_url)

    flask_exts.email_sender.send(
        subject='Please confirm your email',
        recipients=[club_email],
        body=html
    )

    return {'status': 'success'}


@user_blueprint.route('/confirm/<token>', methods=['GET'])
def confirm_email(token):
    club_email = flask_exts.email_verifier.confirm_token(token, 'confirm-email')
    if club_email is None:
        raise JsonError(status='error', reason='The confirmation link is invalid!', status_=404)

    matching_user = User.objects(email=club_email).first()
    if matching_user is None:
        raise JsonError(status='error', reason='The user matching the email does not exist!', status_=404)

    if matching_user.confirmed:
        return redirect(LOGIN_URL)

    matching_user.confirmed = True
    matching_user.confirmed_on = datetime.datetime.now()
    matching_user.save()

    return redirect(LOGIN_URL)


@as_json
@user_blueprint.route('/login', methods=['POST'])
@validate_json(schema={
    'email': {'type': 'string', 'minlength': 1},
    'password': {'type': 'string', 'minlength': 1}
}, require_all=True)
def login():
    json = g.clean_json
    email = json['email']
    password = json['password']

    user = User.objects(email=email).first()
    if user is None:
        raise JsonError(status='error', reason='The user does not exist!')

    if not hash_manager.verify(password, user.password):
        raise JsonError(status='error', reason='The password is incorrect!')

    access_token = create_access_token(identity=email)
    refresh_token = create_refresh_token(identity=email)
    
    access_jti = get_jti(encoded_token=access_token)
    refresh_jti = get_jti(encoded_token=refresh_token)

    AccessJTI(owner=user, token_id=access_jti).save()
    RefreshJTI(owner=user, token_id=refresh_jti).save()

    return {'access': access_token, 'refresh': refresh_token}


@as_json
@user_blueprint.route('/request-reset', methods=['POST'])
@validate_json(schema={
    'email': {'type': 'string', 'minlength': 1}
}, require_all=True)
def request_reset_password():
    json = g.clean_json
    club_email = json['email']
    recover_token = flask_exts.email_verifier.generate_token(club_email, 'reset-password')
    html = render_template('reset-password.html', reset_pass_url=f'{RECOVER_URL}?token={recover_token}')

    flask_exts.email_sender.send(
        subject='Reset your password',
        recipients=[club_email],
        body=html
    )

    return {'status': 'success'}


@as_json
@user_blueprint.route('/confirm-reset', methods=['POST'])
@validate_json(schema={
    'token': {'type': 'string'},
    'password': {'type': 'string'}
}, require_all=True)
def confirm_reset_password():
    json = g.clean_json

    token = json['token']
    password = json['password']

    club_email = flask_exts.email_verifier.confirm_token(token, 'reset-password')
    if club_email is None:
        raise JsonError(status='error', reason='The recovery token is invalid!', status_=404)

    owner = User.objects(email=club_email).first()
    if owner is None:
        raise JsonError(status='error', reason='The user matching the email does not exist!', status_=404)

    # First delete all access and refresh tokens from the user
    AccessJTI.objects(owner=owner).delete()
    RefreshJTI.objects(owner=owner).delete()

    # Next, set the new password
    owner.password = hash_manager.hash(password)
    owner.save()

    return {'status': 'success'}


@as_json
@user_blueprint.route('/refresh', methods=['POST'])
@jwt_refresh_token_required
def refresh():
    owner = current_user['user']
    owner_email = owner.email
    
    access_token = create_access_token(identity=owner_email)
    access_jti = get_jti(access_token)

    AccessJTI(owner=owner, token_id=access_jti).save()
    return {'access': access_token}


@as_json
@user_blueprint.route('/revoke-access', methods=['DELETE'])
@jwt_required
def revoke_access():
    jti = get_raw_jwt()['jti']

    access_jti = AccessJTI.objects(token_id=jti).first()
    if access_jti is None:
        raise JsonError(status='error', reason='Access token does not exist!', status_=404)

    access_jti.expired = True
    access_jti.save()

    return {
        'status': 'success',
        'message': 'Access token revoked!'
    }


@as_json
@user_blueprint.route('/revoke-refresh', methods=['DELETE'])
@jwt_refresh_token_required
def revoke_refresh():
    jti = get_raw_jwt()['jti']
    
    refresh_jti = RefreshJTI.objects(token_id=jti).first()
    if refresh_jti is None:
        raise JsonError(status='error', reason='Refresh token does not exist!', status_=404)

    refresh_jti.expired = True
    refresh_jti.save()

    return {
        'status': 'success',
        'message': 'Refresh token revoked!'
    }
