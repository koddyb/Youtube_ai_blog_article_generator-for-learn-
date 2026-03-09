import json
import os
import re

from django.shortcuts import render
from django.contrib.auth.models import User
from django.contrib.auth import authenticate, login, logout
from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
from django.contrib import messages
from mistralai import Mistral
from .models import BlogPost
# Create your views here.
@login_required
def index(request):
    return render(request, 'index.html') 

@csrf_exempt
def generate_blog(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            yt_link = data['link']
        except (KeyError, json.JSONDecodeError):
            return JsonResponse({'error': 'Invalid data sent.'}, status=400)

        # Check for duplicate link
        existing = BlogPost.objects.filter(user=request.user, youtube_link=yt_link).first()
        if existing:
            return JsonResponse({
                'error': 'duplicate',
                'message': 'You already have an article for this video.',
                'article_id': existing.id
            }, status=400)

        #get yt title
        title = yt_title(yt_link)
        #get transcript
        transcription = get_transcription(yt_link)
        if not transcription:
            return JsonResponse({'error': "Impossible de récupérer la transcription. La vidéo n'a peut-être pas de sous-titres disponibles."}, status=500)
        #use mistral to generate the blog  
        blog_content = generate_blog_from_transcription(transcription)
        if not blog_content:
            return JsonResponse({'error': "Failed to generate the blog article"}, status=500)

        #saving blog article into the database 
        new_blog_article = BlogPost.objects.create(
            user = request.user,
            youtube_title = title,
            youtube_link = yt_link,
            generated_content = blog_content,
        )
        new_blog_article.save()
        #return blog article as a reponse
        return JsonResponse({'content': blog_content})
    else:
        return JsonResponse({'error': 'Invalid Request method.'}, status=405)
    
def extract_video_id(url):
    """Extrait l'ID YouTube depuis n'importe quel format d'URL."""
    patterns = [
        r'(?:v=|\/)([0-9A-Za-z_-]{11})',
        r'youtu\.be\/([0-9A-Za-z_-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

def yt_title(link):
    """Récupère le titre via l'API oEmbed YouTube (sans yt-dlp, sans auth)."""
    import requests as req
    try:
        r = req.get(
            'https://www.youtube.com/oembed',
            params={'url': link, 'format': 'json'},
            timeout=5
        )
        if r.status_code == 200:
            return r.json().get('title', 'YouTube Video')
    except Exception:
        pass
    video_id = extract_video_id(link)
    return f"YouTube Video ({video_id})" if video_id else "YouTube Video"

def _get_cookies_path():
    """Retourne le chemin du fichier cookies si disponible."""
    from django.conf import settings
    cookies_path = os.path.join(settings.BASE_DIR, 'temp_cookies.txt')
    cookies_content = os.getenv('YT_COOKIES_CONTENT')

    if cookies_content:
        with open(cookies_path, 'w') as f:
            f.write(cookies_content)

    return cookies_path if os.path.exists(cookies_path) else None


def _get_transcription_ytdlp(video_id):
    """Récupère la transcription via yt-dlp (meilleur contournement anti-bot)."""
    import subprocess
    import tempfile
    import glob as glob_module
    import logging

    logger = logging.getLogger(__name__)
    url = f"https://www.youtube.com/watch?v={video_id}"

    with tempfile.TemporaryDirectory() as tmpdir:
        cmd = [
            'yt-dlp',
            '--skip-download',
            '--write-subs',
            '--write-auto-subs',
            '--sub-langs', 'fr,en,fr.*,en.*',
            '--sub-format', 'vtt',
            '--no-warnings',
            '--no-check-formats',
            '--ignore-errors',
            '-o', os.path.join(tmpdir, '%(id)s'),
        ]

        cookies_path = _get_cookies_path()
        if cookies_path:
            cmd.extend(['--cookies', cookies_path])

        cmd.append(url)

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60
            )
            # Ne pas se fier au returncode — yt-dlp peut retourner une erreur
            # de format vidéo tout en ayant écrit les sous-titres avec succès
            if result.returncode != 0:
                logger.info(f"yt-dlp returncode={result.returncode}, checking for subtitle files anyway")
                if result.stderr:
                    logger.info(f"yt-dlp stderr: {result.stderr[:500]}")
        except subprocess.TimeoutExpired:
            logger.warning("yt-dlp timeout")
            return None
        except FileNotFoundError:
            logger.error("yt-dlp not found in PATH")
            return None

        # Chercher les fichiers de sous-titres générés
        sub_files = glob_module.glob(os.path.join(tmpdir, '*.vtt'))
        if not sub_files:
            sub_files = glob_module.glob(os.path.join(tmpdir, '*.srt'))
        if not sub_files:
            logger.warning(f"yt-dlp n'a produit aucun fichier de sous-titres pour {video_id}")
            return None

        # Préférer fr > en > premier disponible
        chosen = sub_files[0]
        for sf in sub_files:
            if '.fr.' in sf:
                chosen = sf
                break
            elif '.en.' in sf:
                chosen = sf

        return _parse_vtt(chosen)


def _parse_vtt(filepath):
    """Parse un fichier VTT/SRT et retourne le texte brut sans doublons."""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    lines = content.split('\n')
    text_lines = []
    seen = set()

    for line in lines:
        line = line.strip()
        # Ignorer les headers VTT, lignes de timing, lignes vides et tags
        if not line or line == 'WEBVTT' or '-->' in line or line.startswith('NOTE'):
            continue
        if re.match(r'^\d+$', line):
            continue
        # Nettoyer les tags HTML/VTT
        clean = re.sub(r'<[^>]+>', '', line)
        clean = clean.strip()
        if clean and clean not in seen:
            seen.add(clean)
            text_lines.append(clean)

    return ' '.join(text_lines) if text_lines else None


def _get_transcription_api(video_id):
    """Récupère la transcription via youtube-transcript-api."""
    from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled
    import logging

    logger = logging.getLogger(__name__)
    cookies_path = _get_cookies_path()
    api = YouTubeTranscriptApi()

    try:
        kwargs = {'languages': ['fr', 'en']}
        if cookies_path:
            kwargs['cookies'] = cookies_path
        data = api.fetch(video_id, **kwargs)
        return " ".join([s.text for s in data])
    except (TranscriptsDisabled, NoTranscriptFound) as e:
        logger.warning(f"[transcript-api] Pas de transcript pour {video_id}: {e}")
    except Exception as e:
        logger.error(f"[transcript-api] Erreur fetch pour {video_id}: {type(e).__name__}: {e}")

    # Fallback : lister toutes les langues disponibles
    try:
        list_kwargs = {}
        if cookies_path:
            list_kwargs['cookies'] = cookies_path
        transcript_list = api.list(video_id, **list_kwargs)
        transcripts = list(transcript_list)
        if not transcripts:
            logger.warning(f"[transcript-api] Aucune langue disponible pour {video_id}")
            return None
        data = transcripts[0].fetch()
        return " ".join([s.text for s in data])
    except (TranscriptsDisabled, NoTranscriptFound) as e:
        logger.warning(f"[transcript-api] Transcripts désactivés pour {video_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"[transcript-api] Erreur list/fallback pour {video_id}: {type(e).__name__}: {e}")
        return None


def get_transcription(link):
    """Récupère la transcription avec stratégie multi-fallback.
    
    Stratégie : youtube-transcript-api → yt-dlp (meilleur anti-bot).
    """
    import logging
    logger = logging.getLogger(__name__)

    video_id = extract_video_id(link)
    if not video_id:
        return None

    # Stratégie 1 : youtube-transcript-api (rapide, mais bloqué sur certains serveurs)
    logger.info(f"[Transcription] Essai youtube-transcript-api pour {video_id}")
    result = _get_transcription_api(video_id)
    if result:
        logger.info("[Transcription] Succès via youtube-transcript-api")
        return result

    # Stratégie 2 : yt-dlp (plus lent mais meilleur contournement anti-bot)
    logger.info(f"[Transcription] Fallback yt-dlp pour {video_id}")
    result = _get_transcription_ytdlp(video_id)
    if result:
        logger.info("[Transcription] Succès via yt-dlp")
        return result

    logger.warning(f"[Transcription] Échec total pour {video_id}")
    return None

def generate_blog_from_transcription(transcription):
    api_key = os.getenv("MISTRAL_API_key")
    client = Mistral(api_key=api_key)
    
    prompt = f"""
    Tu es un rédacteur web expert. À partir de la transcription suivante, 
    rédige un article de blog structuré, engageant et optimisé pour le SEO.
    
    Instructions :
    1. Donne un titre accrocheur.
    2. Ajoute une introduction brève.
    3. Utilise des sous-titres (H2, H3) pour structurer le contenu.
    4. Nettoie les tics de langage et les répétitions de la transcription.
    5. Utilise des listes à puces si nécessaire.
    6. Ajoute une conclusion avec un appel à l'action.

    Transcription : {transcription}
    """
    chat_response = client.chat.complete(
        model="mistral-small", # ou (mistral-large-latest) pas oublier de regarder l'itilisation
        messages=[
            {"role": "user", "content": prompt},
        ]
    )
    generated_content = chat_response.choices[0].message.content
    return generated_content

def blog_list(request):
    Blog_articles = BlogPost.objects.filter(user=request.user)
    return render(request, "all-blogs.html" , {'blog_articles': Blog_articles })

# def blog_detail(request, pk):
#     blog_article = BlogPost.objects.get(id=pk)
#     return render(request, "blog-details.html", {'blog_article_detail': blog_article})
    
def blog_details(request, pk):
    blog_article_detail = BlogPost.objects.get(id=pk)
    if request.user != blog_article_detail.user:
        return redirect('/')
    return render(request, 'blog-details.html', {'blog_article_detail': blog_article_detail})

def delete_blog(request, pk):
    if request.method == 'POST':
        article = BlogPost.objects.get(id=pk)
        if request.user == article.user:
            article.delete()
            messages.success(request, 'Article supprimé avec succès.')
    return redirect('blog-list')
    

def user_login(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            messages.success(request, f'Bienvenue, {username} ! Vous êtes connecté.')
            return redirect('/')
        else:
            return render(request, 'login.html', {'error_message': 'Identifiant ou mot de passe incorrect.'}) 
        
    return render(request, 'login.html')

def user_signup(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        email = request.POST.get('email')
        password = request.POST.get('password')
        confirm_password = request.POST.get('confirm_password')
        
        if password != confirm_password:
            return render(request, 'signup.html', {'error_message': 'Les mots de passe ne correspondent pas.'})
        
        try:
            user = User.objects.create_user(username=username, email=email, password=password)
            user.save()
            login(request, user)
            messages.success(request, f'Compte créé avec succès ! Bienvenue, {username} !')
            return redirect('/')
        except Exception as e:
            return render(request, 'signup.html', {'error_message': str(e)})
         
    return render(request, 'signup.html')

def user_logout(request):
    logout(request)
    messages.info(request, 'Vous avez été déconnecté.')
    return redirect('/')

