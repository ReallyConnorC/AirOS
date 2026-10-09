package com.airos.kitchen;

import android.app.Activity;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.speech.tts.TextToSpeech;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;
import java.io.IOException;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;
import java.util.ArrayList;

public class MainActivity extends Activity implements TextToSpeech.OnInitListener {
 private static final String HOST="app.air-kitchen.local";
 private static final Set<String> ASSETS=new HashSet<>(Arrays.asList("index.html","app.js","api.js", "profile.js", "app.webmanifest", "app-icon.png","config.js","style.css","icon.svg"));
 private WebView web;
 private TextToSpeech speech;
 private boolean voiceReady=false;
 private final ArrayList<String> voiceQueue=new ArrayList<>();
 @Override public void onCreate(Bundle saved){
  super.onCreate(saved);
  getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
  if(Build.VERSION.SDK_INT>=26){NotificationManager n=(NotificationManager)getSystemService(NOTIFICATION_SERVICE);n.createNotificationChannel(new NotificationChannel("requests","Kitchen requests",NotificationManager.IMPORTANCE_HIGH));}
  if(Build.VERSION.SDK_INT>=33 && checkSelfPermission("android.permission.POST_NOTIFICATIONS")!=PackageManager.PERMISSION_GRANTED) requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"},1);
  speech=new TextToSpeech(this,this);
  web=new WebView(this);setContentView(web);
  web.getSettings().setJavaScriptEnabled(true);web.getSettings().setDomStorageEnabled(true);
  web.getSettings().setAllowFileAccess(false);web.getSettings().setAllowContentAccess(false);
  web.getSettings().setMixedContentMode(android.webkit.WebSettings.MIXED_CONTENT_NEVER_ALLOW);
  web.addJavascriptInterface(new KitchenBridge(),"AirKitchen");
  web.setWebViewClient(new WebViewClient(){
   @Override public boolean shouldOverrideUrlLoading(WebView view,WebResourceRequest r){return !trusted(r.getUrl());}
   @Override public WebResourceResponse shouldInterceptRequest(WebView view,WebResourceRequest r){
    Uri uri=r.getUrl();
    if(!HOST.equals(uri.getHost()))return null;
    String name=uri.getPath();if(name==null || name.equals("/"))name="/index.html";name=name.substring(1);
    if(!ASSETS.contains(name))return new WebResourceResponse("text/plain","UTF-8",403,"Forbidden",null,null);
    try{return new WebResourceResponse(name.endsWith(".js")?"application/javascript":name.endsWith(".css")?"text/css":name.endsWith(".svg")?"image/svg+xml":name.endsWith(".png")?"image/png":name.endsWith(".webmanifest")?"application/manifest+json":"text/html","UTF-8",getAssets().open(name));}
    catch(IOException e){return new WebResourceResponse("text/plain","UTF-8",404,"Not Found",null,null);}
   }
  });
  web.loadUrl("https://"+HOST+"/?kitchen=1");
 }
 private boolean trusted(Uri u){return "https".equals(u.getScheme())&&HOST.equals(u.getHost());}
 @Override public void onInit(int status){
  if(status==TextToSpeech.SUCCESS){int result=speech.setLanguage(Locale.UK);if(result==TextToSpeech.LANG_MISSING_DATA||result==TextToSpeech.LANG_NOT_SUPPORTED)result=speech.setLanguage(Locale.US);voiceReady=result>=0;
   runOnUiThread(()->{for(String text:voiceQueue)say(text);voiceQueue.clear();if(!voiceReady)Toast.makeText(this,"Install an English voice in Android Text-to-speech settings to hear requests.",Toast.LENGTH_LONG).show();});
  }
 }
 private void say(String text){if(voiceReady)speech.speak(text,TextToSpeech.QUEUE_ADD,null,"kitchen-"+System.nanoTime());else if(voiceQueue.size()<10)voiceQueue.add(text);}
 private class KitchenBridge {
  @JavascriptInterface public void testVoice(){runOnUiThread(()->say("Kitchen announcements are on."));}
  @JavascriptInterface public void announce(String id,String text){
   if(id==null||text==null||text.length()>1000)return;
   runOnUiThread(()->{say(text);NotificationManager n=(NotificationManager)getSystemService(NOTIFICATION_SERVICE);
    Intent open=new Intent(MainActivity.this,MainActivity.class);open.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP|Intent.FLAG_ACTIVITY_SINGLE_TOP);
    PendingIntent intent=PendingIntent.getActivity(MainActivity.this,0,open,PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
    Notification.Builder b=Build.VERSION.SDK_INT>=26?new Notification.Builder(MainActivity.this,"requests"):new Notification.Builder(MainActivity.this);
    b.setSmallIcon(com.airos.kitchen.R.drawable.icon).setContentTitle("New kitchen request").setContentText(text).setStyle(new Notification.BigTextStyle().bigText(text)).setContentIntent(intent).setAutoCancel(true);
    try{n.notify(id.hashCode(),b.build());}catch(SecurityException ignored){} });
  }
 }
 @Override public void onBackPressed(){if(web.canGoBack())web.goBack();else super.onBackPressed();}
 @Override protected void onDestroy(){if(speech!=null){speech.stop();speech.shutdown();}if(web!=null){web.removeJavascriptInterface("AirKitchen");web.destroy();}super.onDestroy();}
}
