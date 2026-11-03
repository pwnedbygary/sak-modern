package com.sakmodern.app;

import android.app.Activity;
import android.content.ClipData;
import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.RectF;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.provider.MediaStore;
import android.provider.OpenableColumns;
import android.text.TextUtils;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.ArrayAdapter;
import android.widget.TextView;
import android.widget.Toast;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends Activity {
    private static final int REQ_KEYS = 10;
    private static final int REQ_MERGED = 11;
    private static final int REQ_BASE = 12;
    private static final int REQ_ADDONS = 13;

    private static final int BG = Color.rgb(11, 15, 20);
    private static final int PANEL = Color.rgb(17, 24, 32);
    private static final int PANEL2 = Color.rgb(23, 33, 43);
    private static final int FG = Color.rgb(230, 237, 243);
    private static final int MUTED = Color.rgb(139, 152, 169);
    private static final int ACCENT = Color.rgb(59, 130, 246);
    private static final int GREEN = Color.rgb(34, 197, 94);
    private static final int AMBER = Color.rgb(245, 158, 11);
    private static final int RED = Color.rgb(248, 81, 73);

    private final ExecutorService io = Executors.newSingleThreadExecutor();
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final List<File> addons = new ArrayList<>();
    private final List<Pkg> packages = new ArrayList<>();
    private final Set<Integer> selected = new HashSet<>();

    private File keysFile;
    private File mergedFile;
    private File baseFile;

    private LinearLayout screenPartitions;
    private LinearLayout screenBundle;
    private LinearLayout screenSettings;
    private ScrollView scrollPartitions;
    private ScrollView scrollBundle;
    private ScrollView scrollSettings;
    private Button tabPartitions;
    private Button tabBundle;
    private Button tabSettings;
    private TextView statusText;
    private TextView mergedInfo;
    private TextView bundleInfo;
    private TextView keysStatus;
    private TextView outputInfo;
    private LinearLayout packageList;
    private PartitionView partitionView;
    private ProgressBar progress;
    private Spinner formatSpinner;
    private boolean busy;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(buildRoot());
        File defaultKeys = new File(getFilesDir(), "keys/prod.keys");
        if (defaultKeys.isFile()) {
            keysFile = defaultKeys;
            keysStatus.setText("Keys loaded: " + defaultKeys.getName());
        }
        showScreen(0);
    }

    // ---------------------------------------------------------------- UI ----

    private View buildRoot() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(BG);

        LinearLayout header = new LinearLayout(this);
        header.setOrientation(LinearLayout.HORIZONTAL);
        header.setGravity(Gravity.CENTER_VERTICAL);
        header.setPadding(dp(18), dp(18), dp(18), dp(8));
        TextView brand = text("SAK", 28, ACCENT, true);
        TextView modern = text(" modern", 16, FG, false);
        header.addView(brand);
        header.addView(modern);
        root.addView(header);

        TextView sub = text("Switch content tools for Android", 12, MUTED, false);
        sub.setPadding(dp(20), 0, dp(20), dp(10));
        root.addView(sub);

        LinearLayout tabs = new LinearLayout(this);
        tabs.setOrientation(LinearLayout.HORIZONTAL);
        tabs.setPadding(dp(12), 0, dp(12), dp(8));
        tabPartitions = tab("Partitions");
        tabBundle = tab("Bundle");
        tabSettings = tab("Settings");
        tabs.addView(tabPartitions, weightParams());
        tabs.addView(tabBundle, weightParams());
        tabs.addView(tabSettings, weightParams());
        root.addView(tabs);

        FrameLayout content = new FrameLayout(this);
        LinearLayout.LayoutParams contentParams = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f);
        root.addView(content, contentParams);

        screenPartitions = buildPartitions();
        screenBundle = buildBundle();
        screenSettings = buildSettings();
        scrollPartitions = wrapScroll(screenPartitions);
        scrollBundle = wrapScroll(screenBundle);
        scrollSettings = wrapScroll(screenSettings);
        content.addView(scrollPartitions);
        content.addView(scrollBundle);
        content.addView(scrollSettings);

        statusText = text("Ready", 10, MUTED, false);
        statusText.setPadding(dp(18), dp(6), dp(18), dp(10));
        statusText.setMaxLines(2);
        statusText.setEllipsize(TextUtils.TruncateAt.END);
        root.addView(statusText);
        return root;
    }

    private LinearLayout buildPartitions() {
        LinearLayout v = column();
        v.addView(title("Partition workspace"));
        v.addView(sub("Open a merged XCI/NSP, tap a block, then extract it."));

        Button open = button("Import merged XCI/NSP", true);
        open.setOnClickListener(x -> pickFile(REQ_MERGED, false));
        v.addView(open, topMargin(8));

        mergedInfo = text("No file loaded", 12, MUTED, false);
        mergedInfo.setPadding(0, dp(8), 0, dp(6));
        v.addView(mergedInfo);

        partitionView = new PartitionView();
        partitionView.setOnPackageSelected(index -> {
            selected.clear();
            selected.add(index);
            refreshPackageChecks();
            statusText.setText("Selected " + packages.get(index).label);
        });
        LinearLayout.LayoutParams pvParams = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(128));
        v.addView(partitionView, pvParams);

        packageList = column();
        packageList.setPadding(0, dp(8), 0, 0);
        v.addView(packageList);

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        Button verify = button("Verify", false);
        verify.setOnClickListener(x -> verifyMerged());
        Button extractSel = button("Extract selected", true);
        extractSel.setOnClickListener(x -> extract(false));
        row.addView(verify, weightParams());
        row.addView(extractSel, weightParams());
        v.addView(row, topMargin(10));

        Button extractAll = button("Extract all packages", false);
        extractAll.setOnClickListener(x -> extract(true));
        v.addView(extractAll, topMargin(6));

        outputInfo = text("Output: app storage / SAK-Output (copied to Downloads after extraction)", 11, MUTED, false);
        outputInfo.setPadding(0, dp(8), 0, 0);
        v.addView(outputInfo);

        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setIndeterminate(true);
        progress.setVisibility(View.GONE);
        v.addView(progress, topMargin(8));
        return v;
    }

    private LinearLayout buildBundle() {
        LinearLayout v = column();
        v.addView(title("Build a bundle"));
        v.addView(sub("Base game + update + DLC in one XCI or NSP."));

        Button baseBtn = button("Choose base XCI/NSP", true);
        baseBtn.setOnClickListener(x -> pickFile(REQ_BASE, false));
        v.addView(baseBtn, topMargin(8));

        bundleInfo = text("No base selected", 12, MUTED, false);
        bundleInfo.setPadding(0, dp(8), 0, dp(4));
        v.addView(bundleInfo);

        Button addBtn = button("Add update/DLC files", false);
        addBtn.setOnClickListener(x -> pickFile(REQ_ADDONS, true));
        v.addView(addBtn, topMargin(4));

        LinearLayout fmtRow = new LinearLayout(this);
        fmtRow.setOrientation(LinearLayout.HORIZONTAL);
        fmtRow.setGravity(Gravity.CENTER_VERTICAL);
        fmtRow.setPadding(0, dp(8), 0, 0);
        TextView fmtLabel = text("Output", 12, FG, false);
        formatSpinner = new Spinner(this);
        ArrayAdapter<String> adapter = new ArrayAdapter<>(this,
                android.R.layout.simple_spinner_dropdown_item, new String[]{"XCI", "NSP"});
        formatSpinner.setAdapter(adapter);
        fmtRow.addView(fmtLabel);
        LinearLayout.LayoutParams sp = new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        sp.leftMargin = dp(12);
        fmtRow.addView(formatSpinner, sp);
        v.addView(fmtRow);

        Button build = button("Build bundle", true);
        build.setOnClickListener(x -> bundle());
        v.addView(build, topMargin(10));

        TextView note = text("The desktop engine handles updates/DLC replacement and 16/32 GiB card sizing. "
                + "Large files need free space; Android copies inputs into app storage.", 11, MUTED, false);
        note.setPadding(0, dp(10), 0, 0);
        v.addView(note);
        return v;
    }

    private LinearLayout buildSettings() {
        LinearLayout v = column();
        v.addView(title("Settings"));
        v.addView(sub("Keys stay on this device."));

        Button keys = button("Choose prod.keys / keys.txt", true);
        keys.setOnClickListener(x -> pickFile(REQ_KEYS, false));
        v.addView(keys, topMargin(10));

        keysStatus = text("No keys loaded", 12, MUTED, false);
        keysStatus.setPadding(0, dp(10), 0, 4);
        v.addView(keysStatus);

        TextView note = text("SAK Modern needs a prod.keys file for NCA header parsing and XCI conversion. "
                + "The file is copied into private app storage. No keys or game content are included in the APK.",
                12, MUTED, false);
        note.setPadding(0, dp(10), 0, 0);
        v.addView(note);

        TextView about = text("SAK Modern 1.0.0\nAndroid front-end: native Java UI + Chaquopy Python core.\n"
                + "Desktop builds: Windows, Linux AppImage.", 11, MUTED, false);
        about.setPadding(0, dp(24), 0, 0);
        v.addView(about);
        return v;
    }

    private void showScreen(int index) {
        scrollPartitions.setVisibility(index == 0 ? View.VISIBLE : View.GONE);
        scrollBundle.setVisibility(index == 1 ? View.VISIBLE : View.GONE);
        scrollSettings.setVisibility(index == 2 ? View.VISIBLE : View.GONE);
        screenPartitions.setVisibility(View.VISIBLE);
        screenBundle.setVisibility(View.VISIBLE);
        screenSettings.setVisibility(View.VISIBLE);
        styleTab(tabPartitions, index == 0);
        styleTab(tabBundle, index == 1);
        styleTab(tabSettings, index == 2);
    }

    private void styleTab(Button b, boolean active) {
        b.setBackground(round(active ? PANEL2 : BG, 10));
        b.setTextColor(active ? FG : MUTED);
    }

    // ------------------------------------------------------------- files ----

    private void pickFile(int request, boolean multiple) {
        Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        i.addCategory(Intent.CATEGORY_OPENABLE);
        i.setType("*/*");
        if (multiple) i.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
        startActivityForResult(i, request);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (resultCode != RESULT_OK || data == null) return;
        if (requestCode == REQ_KEYS) {
            Uri uri = data.getData();
            if (uri != null) importKeys(uri);
        } else if (requestCode == REQ_MERGED) {
            Uri uri = data.getData();
            if (uri != null) importMerged(uri);
        } else if (requestCode == REQ_BASE) {
            Uri uri = data.getData();
            if (uri != null) importBase(uri);
        } else if (requestCode == REQ_ADDONS) {
            List<Uri> uris = new ArrayList<>();
            ClipData clip = data.getClipData();
            if (clip != null) {
                for (int i = 0; i < clip.getItemCount(); i++) uris.add(clip.getItemAt(i).getUri());
            } else if (data.getData() != null) {
                uris.add(data.getData());
            }
            importAddons(uris);
        }
    }

    private void importKeys(Uri uri) {
        setBusy(true, "Copying keys…");
        io.execute(() -> {
            try {
                File dir = new File(getFilesDir(), "keys");
                if (!dir.exists()) dir.mkdirs();
                File out = new File(dir, "prod.keys");
                copyUri(uri, out);
                keysFile = out;
                ui.post(() -> {
                    keysStatus.setText("Keys loaded: " + out.getName() + " (" + human(out.length()) + ")");
                    setBusy(false, "Keys ready");
                });
            } catch (Exception e) {
                fail("Keys import failed", e);
            }
        });
    }

    private void importMerged(Uri uri) {
        setBusy(true, "Copying merged file…");
        io.execute(() -> {
            try {
                File out = copyToInputs(uri);
                mergedFile = out;
                ui.post(() -> {
                    mergedInfo.setText(out.getName() + " · " + human(out.length()));
                    setBusy(false, "Reading packages…");
                    inspectMerged();
                });
            } catch (Exception e) {
                fail("Import failed", e);
            }
        });
    }

    private void importBase(Uri uri) {
        setBusy(true, "Copying base…");
        io.execute(() -> {
            try {
                File out = copyToInputs(uri);
                baseFile = out;
                ui.post(() -> {
                    bundleInfo.setText("Base: " + out.getName() + " · " + human(out.length()));
                    setBusy(false, "Base ready");
                });
            } catch (Exception e) {
                fail("Base import failed", e);
            }
        });
    }

    private void importAddons(List<Uri> uris) {
        if (uris.isEmpty()) return;
        setBusy(true, "Copying " + uris.size() + " file(s)…");
        io.execute(() -> {
            try {
                List<File> copied = new ArrayList<>();
                for (Uri uri : uris) copied.add(copyToInputs(uri));
                ui.post(() -> {
                    addons.addAll(copied);
                    bundleInfo.setText("Base: " + (baseFile == null ? "not selected" : baseFile.getName())
                            + "\nAdd-ons: " + addons.size() + " file(s)");
                    setBusy(false, "Add-ons ready");
                });
            } catch (Exception e) {
                fail("Add-on import failed", e);
            }
        });
    }

    private File copyToInputs(Uri uri) throws Exception {
        File dir = getExternalFilesDir("inputs");
        if (dir == null) dir = new File(getFilesDir(), "inputs");
        if (!dir.exists()) dir.mkdirs();
        String name = displayName(uri);
        if (TextUtils.isEmpty(name)) name = "input.bin";
        File out = uniqueFile(dir, name);
        copyUri(uri, out);
        return out;
    }

    private String displayName(Uri uri) {
        try (Cursor c = getContentResolver().query(uri, null, null, null, null)) {
            if (c != null && c.moveToFirst()) {
                int idx = c.getColumnIndex(OpenableColumns.DISPLAY_NAME);
                if (idx >= 0) return c.getString(idx);
            }
        } catch (Exception ignored) {
        }
        return "input.bin";
    }

    private void copyUri(Uri uri, File out) throws Exception {
        ContentResolver resolver = getContentResolver();
        try (InputStream in = resolver.openInputStream(uri);
             OutputStream os = new FileOutputStream(out)) {
            if (in == null) throw new Exception("Cannot open input stream");
            byte[] buf = new byte[1024 * 1024];
            int n;
            while ((n = in.read(buf)) > 0) os.write(buf, 0, n);
        }
    }

    private File uniqueFile(File dir, String name) {
        File f = new File(dir, name);
        if (!f.exists()) return f;
        String stem = name;
        String ext = "";
        int dot = name.lastIndexOf('.');
        if (dot > 0) {
            stem = name.substring(0, dot);
            ext = name.substring(dot);
        }
        for (int i = 2; i < 10000; i++) {
            f = new File(dir, stem + " (" + i + ")" + ext);
            if (!f.exists()) return f;
        }
        return new File(dir, System.currentTimeMillis() + "-" + name);
    }

    // ------------------------------------------------------------- python ----

    private PyObject py() {
        return Python.getInstance().getModule("sak_android");
    }

    private void inspectMerged() {
        if (mergedFile == null) return;
        if (!ensureKeys()) return;
        setBusy(true, "Reading packages…");
        io.execute(() -> {
            try {
                String json = py().callAttr("inspect", mergedFile.getAbsolutePath(),
                        keysFile.getAbsolutePath()).toString();
                JSONObject obj = new JSONObject(json);
                JSONArray arr = obj.getJSONArray("packages");
                List<Pkg> list = new ArrayList<>();
                for (int i = 0; i < arr.length(); i++) {
                    JSONObject o = arr.getJSONObject(i);
                    list.add(new Pkg(o.getString("id"), o.getString("kind"), o.getString("label"),
                            o.getString("title"), o.optLong("version"), o.getLong("size"), o.optInt("ncas")));
                }
                ui.post(() -> renderPackages(obj.optString("type", "?"), list));
            } catch (Exception e) {
                fail("Inspect failed", e);
            }
        });
    }

    private void renderPackages(String type, List<Pkg> list) {
        packages.clear();
        packages.addAll(list);
        selected.clear();
        partitionView.setPackages(list);
        packageList.removeAllViews();
        for (int i = 0; i < list.size(); i++) {
            Pkg p = list.get(i);
            CheckBox cb = new CheckBox(this);
            cb.setText(p.label + "  " + p.title + "  ·  " + human(p.size));
            cb.setTextColor(FG);
            cb.setTextSize(12);
            cb.setPadding(dp(8), dp(6), dp(8), dp(6));
            cb.setBackground(round(i % 2 == 0 ? PANEL : PANEL2, 8));
            final int index = i;
            cb.setOnCheckedChangeListener((b, checked) -> {
                if (checked) selected.add(index); else selected.remove(index);
                partitionView.setSelected(selected);
            });
            packageList.addView(cb, topMargin(4));
        }
        long total = 0;
        for (Pkg p : list) total += p.size;
        mergedInfo.setText(mergedFile.getName() + " · " + type.toUpperCase() + " · " + list.size()
                + " packages · " + human(total));
        outputInfo.setText("Output: app storage / SAK-Output; finished files are copied to Downloads/SAK-Output.");
        setBusy(false, "Loaded " + list.size() + " packages");
    }

    private void verifyMerged() {
        if (!ensureMerged()) return;
        if (!ensureKeys()) return;
        setBusy(true, "Verifying…");
        io.execute(() -> {
            try {
                String json = py().callAttr("verify", mergedFile.getAbsolutePath(),
                        keysFile.getAbsolutePath()).toString();
                JSONObject o = new JSONObject(json);
                boolean ok = o.getBoolean("ok");
                JSONArray errors = o.optJSONArray("errors");
                int count = errors == null ? 0 : errors.length();
                ui.post(() -> setBusy(false, ok ? "Verification OK" : "Verification failed: "
                        + (errors == null ? "" : errors.optString(0))));
            } catch (Exception e) {
                fail("Verify failed", e);
            }
        });
    }

    private void extract(boolean all) {
        if (!ensureMerged()) return;
        if (!ensureKeys()) return;
        List<String> ids = new ArrayList<>();
        if (all) {
            for (Pkg p : packages) ids.add(p.id);
        } else {
            for (int i : selected) if (i < packages.size()) ids.add(packages.get(i).id);
        }
        if (ids.isEmpty()) {
            toast("Select at least one package first.");
            return;
        }
        File out = new File(getExternalFilesDir(null), "SAK-Output");
        if (!out.exists()) out.mkdirs();
        setBusy(true, "Extracting " + ids.size() + " package(s)…");
        io.execute(() -> {
            try {
                String json = py().callAttr("extract", mergedFile.getAbsolutePath(),
                        out.getAbsolutePath(), keysFile.getAbsolutePath(),
                        ids.toArray(new String[0]), "source").toString();
                JSONArray arr = new JSONArray(json);
                List<File> files = new ArrayList<>();
                for (int i = 0; i < arr.length(); i++) {
                    files.add(new File(arr.getJSONObject(i).getString("path")));
                }
                List<String> saved = new ArrayList<>();
                for (File f : files) {
                    try {
                        String where = saveToDownloads(f);
                        saved.add(f.getName() + (where == null ? "" : " → " + where));
                    } catch (Exception e) {
                        saved.add(f.getName() + " (kept in app storage: " + e.getMessage() + ")");
                    }
                }
                ui.post(() -> setBusy(false, "Extracted " + files.size()
                        + " package(s) → Downloads/SAK-Output"));
            } catch (Exception e) {
                fail("Extraction failed", e);
            }
        });
    }

    private void bundle() {
        if (baseFile == null) {
            toast("Choose a base game first.");
            return;
        }
        if (!ensureKeys()) return;
        File out = new File(getExternalFilesDir(null), "SAK-Output");
        if (!out.exists()) out.mkdirs();
        String fmt = formatSpinner.getSelectedItemPosition() == 0 ? "xci" : "nsp";
        setBusy(true, "Bundling…");
        io.execute(() -> {
            try {
                List<String> addPaths = new ArrayList<>();
                for (File f : addons) addPaths.add(f.getAbsolutePath());
                String json = py().callAttr("bundle", baseFile.getAbsolutePath(),
                        addPaths.toArray(new String[0]), out.getAbsolutePath(), fmt,
                        keysFile.getAbsolutePath()).toString();
                File result = new File(new JSONObject(json).getString("path"));
                String where = saveToDownloads(result);
                ui.post(() -> setBusy(false, "Bundle created: " + result.getName()
                        + (where == null ? "" : "\n" + where)));
            } catch (Exception e) {
                fail("Bundle failed", e);
            }
        });
    }

    private void saveToDownloadsAsync(File file, Callback cb) {
        io.execute(() -> {
            try {
                cb.done(saveToDownloads(file));
            } catch (Exception e) {
                cb.done("Could not copy: " + e.getMessage());
            }
        });
    }

    private String saveToDownloads(File file) throws Exception {
        if (Build.VERSION.SDK_INT >= 29) {
            ContentValues cv = new ContentValues();
            cv.put(MediaStore.MediaColumns.DISPLAY_NAME, file.getName());
            cv.put(MediaStore.MediaColumns.MIME_TYPE, "application/octet-stream");
            cv.put(MediaStore.MediaColumns.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/SAK-Output");
            Uri uri = getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, cv);
            if (uri == null) throw new Exception("MediaStore insert failed");
            try (InputStream in = new FileInputStream(file);
                 OutputStream os = getContentResolver().openOutputStream(uri)) {
                if (os == null) throw new Exception("Cannot open Downloads output");
                byte[] buf = new byte[1024 * 1024];
                int n;
                while ((n = in.read(buf)) > 0) os.write(buf, 0, n);
            }
            return "Downloads/SAK-Output/" + file.getName();
        }
        if (checkSelfPermission(android.Manifest.permission.WRITE_EXTERNAL_STORAGE)
                != PackageManager.PERMISSION_GRANTED) {
            throw new Exception("storage permission required");
        }
        File dir = new File(Environment.getExternalStoragePublicDirectory(
                Environment.DIRECTORY_DOWNLOADS), "SAK-Output");
        if (!dir.exists()) dir.mkdirs();
        File dst = new File(dir, file.getName());
        try (InputStream in = new FileInputStream(file);
             OutputStream os = new FileOutputStream(dst)) {
            byte[] buf = new byte[1024 * 1024];
            int n;
            while ((n = in.read(buf)) > 0) os.write(buf, 0, n);
        }
        return dst.getAbsolutePath();
    }

    // ------------------------------------------------------------- helpers ----

    private boolean ensureKeys() {
        if (keysFile != null && keysFile.isFile()) return true;
        toast("Choose prod.keys in Settings first.");
        showScreen(2);
        return false;
    }

    private boolean ensureMerged() {
        if (mergedFile != null && mergedFile.isFile()) return true;
        toast("Import a merged XCI/NSP first.");
        return false;
    }

    private void setBusy(boolean value, String message) {
        busy = value;
        progress.setVisibility(value ? View.VISIBLE : View.GONE);
        statusText.setText(message);
        if (!value) {
            progress.setIndeterminate(false);
            progress.setProgress(100);
        }
    }

    private void fail(String prefix, Exception e) {
        ui.post(() -> {
            setBusy(false, prefix + ": " + e.getMessage());
            toast(prefix);
        });
    }

    private void toast(String message) {
        Toast.makeText(this, message, Toast.LENGTH_LONG).show();
    }

    private void refreshPackageChecks() {
        for (int i = 0; i < packageList.getChildCount(); i++) {
            View child = packageList.getChildAt(i);
            if (child instanceof CheckBox) ((CheckBox) child).setChecked(selected.contains(i));
        }
    }

    private TextView title(String s) {
        TextView v = text(s, 24, FG, true);
        v.setPadding(0, dp(8), 0, dp(2));
        return v;
    }

    private TextView sub(String s) {
        TextView v = text(s, 12, MUTED, false);
        v.setPadding(0, 0, 0, dp(4));
        return v;
    }

    private TextView text(String s, int sp, int color, boolean bold) {
        TextView v = new TextView(this);
        v.setText(s);
        v.setTextSize(sp);
        v.setTextColor(color);
        if (bold) v.setTypeface(v.getTypeface(), android.graphics.Typeface.BOLD);
        return v;
    }

    private Button tab(String s) {
        Button b = new Button(this);
        b.setText(s);
        b.setTextSize(13);
        b.setAllCaps(false);
        b.setTextColor(MUTED);
        b.setBackground(round(BG, 10));
        b.setOnClickListener(x -> showScreen(s.equals("Partitions") ? 0 : s.equals("Bundle") ? 1 : 2));
        return b;
    }

    private Button button(String s, boolean accent) {
        Button b = new Button(this);
        b.setText(s);
        b.setTextSize(13);
        b.setAllCaps(false);
        b.setTextColor(Color.WHITE);
        b.setBackground(round(accent ? ACCENT : PANEL2, 12));
        b.setPadding(dp(14), dp(10), dp(14), dp(10));
        b.setMinHeight(dp(44));
        return b;
    }

    private LinearLayout column() {
        LinearLayout v = new LinearLayout(this);
        v.setOrientation(LinearLayout.VERTICAL);
        v.setPadding(dp(16), dp(8), dp(16), dp(24));
        return v;
    }

    private ScrollView wrapScroll(View child) {
        ScrollView s = new ScrollView(this);
        s.setFillViewport(true);
        s.addView(child, new ScrollView.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        return s;
    }

    private LinearLayout.LayoutParams weightParams() {
        return new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
    }

    private LinearLayout.LayoutParams topMargin(int dp) {
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        p.topMargin = dp(dp);
        return p;
    }

    private GradientDrawable round(int color, int radius) {
        GradientDrawable d = new GradientDrawable();
        d.setColor(color);
        d.setCornerRadius(dp(radius));
        return d;
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private String human(long n) {
        double v = n;
        String[] units = {"B", "KiB", "MiB", "GiB", "TiB"};
        int i = 0;
        while (v >= 1024 && i < units.length - 1) {
            v /= 1024;
            i++;
        }
        return String.format(Locale.US, "%.2f %s", v, units[i]);
    }

    private interface Callback {
        void done(String value);
    }

    // ------------------------------------------------------------- views ----

    static class Pkg {
        final String id;
        final String kind;
        final String label;
        final String title;
        final long version;
        final long size;
        final int ncas;

        Pkg(String id, String kind, String label, String title, long version, long size, int ncas) {
            this.id = id;
            this.kind = kind;
            this.label = label;
            this.title = title;
            this.version = version;
            this.size = size;
            this.ncas = ncas;
        }

        int color() {
            if ("base".equals(kind)) return ACCENT;
            if ("update".equals(kind)) return AMBER;
            return Color.rgb(34, 197, 94);
        }
    }

    interface OnPackageSelected {
        void onSelected(int index);
    }

    class PartitionView extends View {
        private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint textPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final RectF rect = new RectF();
        private List<Pkg> items = new ArrayList<>();
        private Set<Integer> selected = new HashSet<>();
        private OnPackageSelected listener;

        PartitionView() {
            super(MainActivity.this);
            setLayerType(View.LAYER_TYPE_SOFTWARE, null);
            textPaint.setColor(Color.WHITE);
            textPaint.setTextSize(dp(11));
            textPaint.setTypeface(android.graphics.Typeface.DEFAULT_BOLD);
        }

        void setPackages(List<Pkg> list) {
            items = list;
            selected.clear();
            invalidate();
        }

        void setSelected(Set<Integer> values) {
            selected = new HashSet<>(values);
            invalidate();
        }

        void setOnPackageSelected(OnPackageSelected l) {
            listener = l;
        }

        @Override
        protected void onDraw(Canvas canvas) {
            super.onDraw(canvas);
            if (items.isEmpty()) {
                paint.setColor(MUTED);
                paint.setTextSize(dp(12));
                paint.setTextAlign(Paint.Align.CENTER);
                canvas.drawText("Import a merged file to see partitions",
                        getWidth() / 2f, getHeight() / 2f, paint);
                return;
            }
            long total = 0;
            for (Pkg p : items) total += Math.max(p.size, 1);
            float left = dp(4);
            float top = dp(10);
            float bottom = getHeight() - dp(28);
            float avail = getWidth() - dp(8);
            float[] widths = new float[items.size()];
            float sum = 0;
            for (int i = 0; i < items.size(); i++) {
                widths[i] = Math.max(dp(9), avail * Math.max(items.get(i).size, 1) / (float) total);
                sum += widths[i];
            }
            float scale = avail / sum;
            for (int i = 0; i < items.size(); i++) {
                Pkg p = items.get(i);
                float w = Math.max(dp(6), widths[i] * scale);
                rect.set(left, top, left + w, bottom);
                paint.setColor(p.color());
                canvas.drawRoundRect(rect, dp(5), dp(5), paint);
                if (selected.contains(i)) {
                    paint.setStyle(Paint.Style.STROKE);
                    paint.setStrokeWidth(dp(2));
                    paint.setColor(Color.WHITE);
                    canvas.drawRoundRect(rect, dp(5), dp(5), paint);
                    paint.setStyle(Paint.Style.FILL);
                }
                if (w > dp(72)) {
                    canvas.save();
                    canvas.clipRect(rect);
                    canvas.drawText(p.label, left + dp(7), top + dp(18), textPaint);
                    textPaint.setTextSize(dp(9));
                    canvas.drawText(human(p.size), left + dp(7), top + dp(32), textPaint);
                    textPaint.setTextSize(dp(11));
                    canvas.restore();
                }
                left += w;
            }
            paint.setColor(MUTED);
            paint.setTextSize(dp(9));
            paint.setTextAlign(Paint.Align.LEFT);
            canvas.drawText("0", dp(4), getHeight() - dp(8), paint);
            paint.setTextAlign(Paint.Align.RIGHT);
            canvas.drawText(human(total), getWidth() - dp(4), getHeight() - dp(8), paint);
        }

        @Override
        public boolean onTouchEvent(MotionEvent event) {
            if (event.getAction() == MotionEvent.ACTION_UP && !items.isEmpty()) {
                long total = 0;
                for (Pkg p : items) total += Math.max(p.size, 1);
                float left = dp(4);
                float avail = getWidth() - dp(8);
                float[] widths = new float[items.size()];
                float sum = 0;
                for (int i = 0; i < items.size(); i++) {
                    widths[i] = Math.max(dp(9), avail * Math.max(items.get(i).size, 1) / (float) total);
                    sum += widths[i];
                }
                float scale = avail / sum;
                for (int i = 0; i < items.size(); i++) {
                    float w = Math.max(dp(6), widths[i] * scale);
                    if (event.getX() >= left && event.getX() <= left + w) {
                        selected.clear();
                        selected.add(i);
                        if (listener != null) listener.onSelected(i);
                        invalidate();
                        performClick();
                        return true;
                    }
                    left += w;
                }
            }
            return true;
        }

        @Override
        public boolean performClick() {
            super.performClick();
            return true;
        }
    }
}
