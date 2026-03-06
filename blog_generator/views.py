import json
import os
import json
import assemblyai as aai
import yt_dlp

from django.shortcuts import render
from django.contrib.auth.models import User
from django.contrib.auth import authenticate, login, logout
from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
from django.conf import settings
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
            return JsonResponse({'error': "Failed to get transcript"}, status=500)
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
    
def yt_title(link):
    with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
        info = ydl.extract_info(link, download=False)
    return info.get('title', '')

def download_audio(link):
    output_template = os.path.join(settings.MEDIA_ROOT, '%(title)s.%(ext)s')
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': output_template,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
        }],
        'quiet': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(link, download=True)
        filename = ydl.prepare_filename(info)
    base, _ = os.path.splitext(filename)
    return base + '.mp3'

def get_transcription(link):
    audio_file = download_audio(link) 
    aai.settings.api_key = os.getenv("ASSEMBLYAI_API_KEY")

    config = aai.TranscriptionConfig(speech_models=["universal-2"])
    transcriber = aai.Transcriber()
    transcript = transcriber.transcribe(audio_file, config=config)

    try:
        os.remove(audio_file)
    except OSError:
        pass
    
    return transcript.text

def generate_blog_from_transcription(transcription):
    api_key = os.getenv("MISTRAL_API_key")
    client = Mistral(api_key=api_key)
    
    prompt = f"""
    Tu es un rédacteur web expert. À partir de la transcription suivante, 
    rédige un article de blog structuré, pas trop long engageant et optimisé pour le SEO.
    
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
        model="mistral-small", # or mistral-large-latest
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
    return redirect('blog-list')
    

def user_login(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            return redirect('/')
        else:
            return render(request, 'login.html', {'error_message': 'Invalid username or password.'}) 
        
    return render(request, 'login.html')

def user_signup(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        email = request.POST.get('email')
        password = request.POST.get('password')
        confirm_password = request.POST.get('confirm_password')
        
        if password != confirm_password:
            return render(request, 'signup.html', {'error_message': 'Passwords do not match.'})
        
        try:
            user = User.objects.create_user(username=username, email=email, password=password)
            user.save()
            login(request, user)
            return redirect('/')
        except Exception as e:
            return render(request, 'signup.html', {'error_message': str(e)})
         
    return render(request, 'signup.html')

def user_logout(request):
    logout(request)
    return redirect('/')

