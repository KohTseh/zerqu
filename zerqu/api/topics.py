# coding: utf-8

from flask import current_app
from flask import request, jsonify

from zerqu.libs.errors import APIException, Conflict, NotFound, Denied
from .base import ApiBlueprint
from .base import require_oauth
from .utils import cursor_query, pagination_query, int_or_raise
from ..models import db, current_user, User
from ..models import Cafe, CafeMember
from ..models import Topic, TopicLike, Comment, TopicRead, TopicStatus
from ..models.topic import topic_list_with_statuses
from ..rec.timeline import get_timeline_topics, get_all_topics
from ..forms import TopicForm, CommentForm
from ..libs.renderer import markup
from ..libs.cache import cache

api = ApiBlueprint('topics')


def get_topic_cafe(cafe_id):
    cafe = Cafe.cache.get_or_404(cafe_id)
    if not cafe.has_read_permission(current_user.id):
        raise Denied('viewing this topic')
    return cafe


@api.route('/timeline')
@require_oauth(login=False, cache_time=600)
def timeline():
    cursor = int_or_raise('cursor', 0)
    if request.args.get('show') == 'all':
        data, cursor = get_all_topics(cursor)
    else:
        data, cursor = get_timeline_topics(cursor, current_user.id)
    reference = {
        'user': User.cache.get_dict({o.user_id for o in data}),
        'cafe': Cafe.cache.get_dict({o.cafe_id for o in data}),
    }
    data = list(Topic.iter_dict(data, **reference))
    data = topic_list_with_statuses(data, current_user.id)
    return jsonify(data=data, cursor=cursor)


@api.route('/statuses')
@require_oauth(login=False, cache_time=600)
def view_statuses():
    id_list = request.args.get('topics')
    if not id_list:
        raise APIException(description='Require parameter "topics" missing')
    try:
        tids = [int(i) for i in id_list.split(',')]
    except ValueError:
        raise APIException(
            description='Require int type on "topics" parameter'
        )
    user_id = None
    if current_user:
        user_id = current_user.id
    return jsonify(Topic.get_multi_statuses(tids, user_id))


@api.route('/<int:tid>')
@require_oauth(login=False, cache_time=600)
def view_topic(tid):
    topic = Topic.cache.get_or_404(tid)
    cafe = get_topic_cafe(topic.cafe_id)

    data = dict(topic)

    # /api/topic/:id?content=raw vs ?content=html
    content_format = request.args.get('content')
    if content_format == 'raw':
        data['content'] = topic.content
    else:
        data['content'] = topic.get_html_content()
        TopicStatus.increase(topic.id, 'views')

    data['user'] = dict(topic.user)
    data['cafe'] = dict(cafe)
    data.update(topic.get_statuses(current_user.id))

    permission = {}
    if current_user and current_user.id == topic.user_id:
        valid = current_app.config.get('ZERQU_VALID_MODIFY_TIME')
        permission['write'] = topic.is_changeable(valid)

    data['permission'] = permission
    return jsonify(data)


@api.route('/<int:tid>', methods=['POST'])
@require_oauth(login=True, scopes=['topic:write'])
def update_topic(tid):
    topic = Topic.query.get(tid)
    if not topic:
        raise NotFound('Topic')

    # who can update topic
    if current_user.id != topic.user_id:
        raise Denied('updating this topic')

    # update topic in the given time
    valid = current_app.config.get('ZERQU_VALID_MODIFY_TIME')
    if not topic.is_changeable(valid):
        msg = 'Topic can only be updated in {} seconds'.format(valid)
        raise APIException(code=403, description=msg)

    form = TopicForm.create_api_form(obj=topic)
    data = dict(form.update_topic())
    data['user'] = dict(current_user)
    data['content'] = topic.get_html_content()
    return jsonify(data)


@api.route('/<int:tid>/read', methods=['POST'])
@require_oauth(login=True)
def write_read_percent(tid):
    topic = Topic.cache.get_or_404(tid)
    percent = request.get_json().get('percent')
    if not isinstance(percent, int):
        raise APIException(description='Invalid payload "percent"')
    read = TopicRead.query.get((topic.id, current_user.id))
    if not read:
        get_topic_cafe(topic.cafe_id)
        read = TopicRead(topic_id=topic.id, user_id=current_user.id)
        TopicStatus.increase(topic.id, 'reads')
    read.percent = percent

    with db.auto_commit():
        db.session.add(read)
    return jsonify(percent=read.percent)


@api.route('/<int:tid>/flag', methods=['POST'])
@require_oauth(login=True)
def flag_topic(tid):
    key = 'flag:%d:t-%d' % (current_user.id, tid)
    if cache.get(key):
        return '', 204
    topic = Topic.cache.get_or_404(tid)
    get_topic_cafe(topic.cafe_id)
    cache.inc(key)

    TopicStatus.increase(topic.id, 'flags')
    return '', 204


@api.route('/<int:tid>/comments')
@require_oauth(login=False, cache_time=600)
def view_topic_comments(tid):
    topic = Topic.cache.get_or_404(tid)
    get_topic_cafe(topic.cafe_id)

    comments, cursor = cursor_query(
        Comment, lambda q: q.filter_by(topic_id=topic.id)
    )
    reference = {'user': User.cache.get_dict({o.user_id for o in comments})}
    data = []
    for d in Comment.iter_dict(comments, **reference):
        d['content'] = markup(d['content'])
        data.append(d)
    return jsonify(data=data, cursor=cursor)


@api.route('/<int:tid>/comments', methods=['POST'])
@require_oauth(login=True, scopes=['comment:write'])
def create_topic_comment(tid):
    topic = Topic.cache.get_or_404(tid)
    cafe = get_topic_cafe(topic.cafe_id)
    # take a record for cafe membership
    CafeMember.get_or_create(cafe.id, current_user.id)

    form = CommentForm.create_api_form()
    comment = form.create_comment(current_user.id, topic.id)
    rv = dict(comment)
    rv['content'] = markup(rv['content'])
    rv['user'] = dict(current_user)

    TopicStatus.increase(topic.id, 'comments')
    return jsonify(rv), 201


@api.route('/<int:tid>/likes')
@require_oauth(login=False, cache_time=600)
def view_topic_likes(tid):
    topic = Topic.cache.get_or_404(tid)

    data, pagination = pagination_query(
        TopicLike, TopicLike.created_at, topic_id=topic.id
    )
    user_ids = [o.user_id for o in data]

    # make current user at the very first position of the list
    current_info = current_user and pagination.page == 1
    if current_info and current_user.id in user_ids:
        user_ids.remove(current_user.id)

    data = User.cache.get_many(user_ids)
    if current_info and TopicLike.cache.get((topic.id, current_user.id)):
        data.insert(0, current_user)
    return jsonify(data=data, pagination=dict(pagination))


@api.route('/<int:tid>/likes', methods=['POST'])
@require_oauth(login=True)
def like_topic(tid):
    data = TopicLike.query.get((tid, current_user.id))
    if data:
        raise Conflict(description='You already liked it')

    topic = Topic.cache.get_or_404(tid)
    get_topic_cafe(topic.cafe_id)
    like = TopicLike(topic_id=topic.id, user_id=current_user.id)
    with db.auto_commit():
        db.session.add(like)

    TopicStatus.increase(topic.id, 'likes')
    return '', 204


@api.route('/<int:tid>/likes', methods=['DELETE'])
@require_oauth(login=True)
def unlike_topic(tid):
    data = TopicLike.query.get((tid, current_user.id))
    if not data:
        raise Conflict(description='You already unliked it')
    with db.auto_commit():
        db.session.delete(data)

    status = TopicStatus.query.get(tid)
    with db.auto_commit(False):
        status.calculate()
    return '', 204


@api.route('/<int:tid>/comments/<int:cid>', methods=['DELETE'])
@require_oauth(login=True, scopes=['comment:write'])
def delete_topic_comment(tid, cid):
    comment = get_comment_or_404(tid, cid)
    if comment.user_id != current_user.id:
        raise Denied('deleting this comment')
    with db.auto_commit():
        db.session.delete(comment)

    status = TopicStatus.query.get(tid)
    with db.auto_commit(False):
        status.calculate()
    return '', 204


@api.route('/<int:tid>/comments/<int:cid>/flag', methods=['POST'])
@require_oauth(login=True)
def flag_topic_comment(tid, cid):
    key = 'flag:%d:c-%d' % (current_user.id, cid)
    if cache.get(key):
        return '', 204
    comment = get_comment_or_404(tid, cid)
    # here is a concurrency bug, but it doesn't matter
    comment.flag_count += 1
    with db.auto_commit():
        db.session.add(comment)
    # one person, one flag
    cache.inc(key)
    return '', 204


def get_comment_or_404(tid, cid):
    comment = Comment.query.get(cid)
    if not comment or comment.topic_id != tid:
        raise NotFound('Comment')
    return comment
